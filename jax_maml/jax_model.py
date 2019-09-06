import random
import copy
import time
import os
import datetime
import math
import warnings

from tensorboardX import SummaryWriter

from typing import Any, Tuple, List, Dict

from abc import ABC, abstractmethod

import numpy as onp
import matplotlib.pyplot as plt

from utils.priority import PriorityQueue

import jax.numpy as np
import matplotlib.pyplot as plt
import jax

# jax imports
from jax import vmap # for auto-vectorizing functions
from jax import jit # for compiling functions for speedup
from jax.experimental import stax # neural network library
from jax.experimental.stax import Conv, Dense, MaxPool, Relu, Flatten, LogSoftmax
from jax import random
from jax.experimental import optimizers

from functools import partial # for use with vmap


class MAML(ABC):

    def __init__(self, params):
        self.params = params

        # extract relevant parameters
        self.task_batch_size = self.params.get("task_batch_size")
        self.inner_update_lr = self.params.get("inner_update_lr")
        self.meta_lr = self.params.get("meta_lr")
        self.inner_update_k = self.params.get("inner_update_k")
        self.validation_k = self.params.get("validation_k")
        self.test_k = self.params.get("test_k")
        self.num_inner_updates = self.params.get("num_inner_updates")
        self.validation_num_inner_updates = self.params.get("validation_num_inner_updates")
        self.training_iterations = self.params.get("training_iterations")
        self.validation_frequency = self.params.get("validation_frequency")
        self.visualisation_frequency = self.params.get("visualisation_frequency")
        self.visualise_all = self.params.get("visualise_all")
        self.checkpoint_path = self.params.get("checkpoint_path")
        self.validation_task_batch_size = self.params.get("validation_task_batch_size")
        self.fixed_validation = self.params.get("fixed_validation")
        self.priority_sample = self.params.get("priority_sample")
        self.input_dimension = self.params.get("input_dimension")
        self.framework = self.params.get("framework")
        self.network_layers = self.params.get("network_layers")
        self.output_dimension = self.params.get("output_dimension")
        self.sample_type = self.params.get(["priority_queue", "sample_type"])

        # initialise tensorboard writer
        self.writer = SummaryWriter(self.checkpoint_path)
        # 'results/{}/{}'.format(self.params.get("experiment_name"), self.params.get("experiment_timestamp"))

        # if using priority queue for inner loop sampling, initialise 
        if self.params.get("priority_sample"):
            self.priority_queue = self._get_priority_queue()

        # write copy of config_yaml in model_checkpoint_folder
        self.params.save_configuration(self.checkpoint_path)

        self.network_initialisation, self.network_forward = self._get_model()
        input_shape = (-1, self.input_dimension,)
        random_initialisation = random.PRNGKey(0)

        # load previously trained model to continue with
        if self.params.get(["resume", "model"]):
            model_checkpoint_path = self.params.get(["resume", "model"])
            try:
                print("Loading and resuming training from checkpoint @ {}".format(model_checkpoint_path))
                model_checkpoint = np.load(model_checkpoint_path, allow_pickle=True)[()]
                self.start_iteration = model_checkpoint["step"] #.split('_')[-1].split('.')[0])
                network_parameters = model_checkpoint["network_parameters"]
            except:
                raise FileNotFoundError("Resume checkpoint specified in config does not exist.")
        else:
            self.start_iteration = 0
            output_shape, network_parameters = self.network_initialisation(random_initialisation, input_shape)

        self.optimier_initialisation, self.optimiser_update, self.get_params_from_optimiser = self._get_optimiser()
        self.optimiser_state = self.optimier_initialisation(network_parameters)

    @abstractmethod
    def _get_model(self):
        """
        Return jax network
        """
        raise NotImplementedError("Base class method")

    def _checkpoint_model(self, step_count: int, network_parameters: List) -> None:
        """
        Save a copy of the network parameters up to this point in training

        :param step_count: iteration number of training (meta-steps)
        """
        os.makedirs(self.checkpoint_path, exist_ok=True)
        timestamp = datetime.datetime.fromtimestamp(time.time()).strftime('%H-%M-%S')
        # format of model chekcpoint path: timestamp _ step_count
        PATH = '{}model_checkpoint_{}_{}.npy'.format(self.checkpoint_path, timestamp, str(step_count))
        np.save(PATH, {
            'step': step_count,
            'network_parameters': network_parameters
            })
        
    @abstractmethod
    def _get_optimiser(self):
        """
        Return jax optimiser
        """
        raise NotImplementedError("Base class method")

    @abstractmethod
    def _get_priority_queue(self):
        """Initiate priority queue"""
        raise NotImplementedError("Base class method")

    @abstractmethod
    def _sample_task(self) -> Any:
        """
        Sample specific task from defined distribution of tasks 
        E.g. one specific sine function from family of sines

        Return type dependent of task family
        """
        raise NotImplementedError("Base class abstract method")

    @abstractmethod
    def _get_task_from_params(self) -> Any:
        """
        Get specific task from specific given parameters 
        E.g. one specific sine function from family of sines

        :param parameters: parameters defining the specific task in the distribution

        Return type dependent of task family

        (method differs from _sample_task in that it is not a random sample but
        defined by parameters given)
        """
        raise NotImplementedError("Base class abstract method")

    @abstractmethod
    def _generate_batch(self, task: Any, batch_size: int=25) -> Tuple:
        """
        Obtain batch of training examples from a sampled task

        :param task: specific task from which to sample x, y pairs
        :param batch_size: number of x, y pairs to sample for batch
        """
        raise NotImplementedError("Base class abstract method")

    @abstractmethod
    def _compute_loss(self):
        """ 
        Compute loss for prediction based on ground truth

        :param prediction: output of network for x
        :param ground_trugh: y value ground truth associated with x
        """
        raise NotImplementedError("Base class abstract method")

    def inner_loop_update(self, parameters, x_batch, y_batch):
        """
        Inner loop of MAML algorithm, consists of optimisation steps on sampled tasks

        :return updated_inner_parameters: updated inner network parameters
        """
        gradients = jax.grad(self._compute_loss)(parameters, x_batch, y_batch)
        inner_sgd_fn = lambda g, state: (state - self.inner_update_lr * g)
        updated_inner_parameters = jax.tree_util.tree_multimap(inner_sgd_fn, gradients, parameters) # TODO (and docstring)
        return updated_inner_parameters

    def _maml_loss(self, parameters, x_batch, y_batch, x_meta, y_meta, task_probability_weights):
        for _ in range(self.num_inner_updates):
            parameters = self.inner_loop_update(parameters, x_batch, y_batch)
        if task_probability_weights is not None:
            loss_for_meta_update = task_probability_weights * self._compute_loss(parameters, x_meta, y_meta)
        else:
            loss_for_meta_update = self._compute_loss(parameters, x_meta, y_meta)
        return loss_for_meta_update

    def batch_maml_loss(self, parameters, x_batch, y_batch, x_meta, y_meta, task_probability_weights, get_all_losses=False):
        task_losses = vmap(partial(self._maml_loss, parameters))(x_batch, y_batch, x_meta, y_meta, task_probability_weights)
        if get_all_losses:
            return task_losses
        return np.mean(task_losses)

    def outer_training_loop(self, step_count: int, optimiser_state, x_batch: np.array, y_batch: np.array, x_meta: np.array, y_meta: np.array, task_probability_weights: np.array):
        """
        Outer loop of MAML algorithm, consists of multiple inner loops and a meta update step

        :param step_count: iteration number
        :param optimiser_state: current state of optimiser
        :param x_batch: input data for batch on which to train inner loop
        :param y_batch: ground truth of batch label values 
        :param x_meta: extra input data sample for meta backprop
        :param y_meta: labels for extra input data
        :param max_indices: for use with priority sample, gives indices of queue used in task batch
        :param task_probability_weights: weights for individual task losses

        :return updated_optimiser
        :return meta_loss
        """
        # get parameters of current state of outer model
        parameters = self.get_params_from_optimiser(optimiser_state)

        # take derivative of inner loss term wrt outer model parameters (automatically wrt 'parameters' via jax.grad as 'parameters' is 1st arg of maml_loss)
        derivative_fn = jax.grad(self.batch_maml_loss)

        # evaluate derivative fn
        gradients = derivative_fn(parameters, x_batch, y_batch, x_meta, y_meta, task_probability_weights)

        # make step in outer model optimiser
        updated_optimiser = self.optimiser_update(step_count, gradients, optimiser_state)

        return updated_optimiser, parameters

    def fast_outer_training_loop(self):
        """
        jit accelerated outer loop method
        """
        return jit(self.outer_training_loop)

    def train(self):
        """
        Training orchestration method, calls outer loop and validation methods
        """
        print("Training starting...")
        for step_count in range(self.start_iteration, self.start_iteration + self.training_iterations):
            # print("Training Step: {}".format(step_count))
            if step_count % self.validation_frequency == 0 and step_count != 0:
                if self.checkpoint_path:
                    current_network_parameters = self.get_params_from_optimiser(self.optimiser_state)
                    self._checkpoint_model(step_count=step_count, network_parameters=current_network_parameters)
                if self.priority_sample:
                    self.priority_queue.save_queue(step_count=step_count)
                if step_count % self.visualisation_frequency == 0:
                    vis = True
                else:
                    vis = False
                self.validate(step_count=step_count, visualise=vis)

            batch_of_tasks, max_indices, task_probabilities = self._sample_task(batch_size=self.task_batch_size, step_count=step_count)

            if 'importance' in self.sample_type:
                standard_task_probabilities = (1. / onp.prod(self.priority_queue.get_queue().shape)) * onp.ones(self.task_batch_size) 
                task_importance_weights = standard_task_probabilities / task_probabilities
            else:
                task_importance_weights = None

            x_train, y_train = self._generate_batch(batch_of_tasks)
            x_meta, y_meta = self._generate_batch(batch_of_tasks)

            self.optimiser_state, parameters = self.fast_outer_training_loop()(step_count, self.optimiser_state, x_train, y_train, x_meta, y_meta, task_probability_weights=task_importance_weights)
            
            # get a validation loss (mostly for logging purposes)
            meta_loss = onp.asarray(self.batch_maml_loss(parameters, x_train, y_train, x_meta, y_meta, task_probability_weights=task_importance_weights, get_all_losses=True))
            
            if self.priority_sample:
                for t in range(len(meta_loss)):
                    self.priority_queue.insert(key=max_indices[t], data=meta_loss[t])

            self.writer.add_scalar('meta_metrics/meta_update_loss_mean', float(np.mean(meta_loss)), step_count)
            self.writer.add_scalar('meta_metrics/meta_update_loss_std', float(np.std(meta_loss)), step_count)

        net_params = self.get_params_from_optimiser(self.optimiser_state)

    def validate(self, step_count: int, visualise: bool=True) -> None:
        """
        Performs a validation step for loss during training

        :param step_count: number of steps in training undergone (used for pring statement)
        :param visualise: whether or not to visualise validation run
        """
        
        validation_losses = []
        validation_figures = []

        validation_parameter_tuples, validation_tasks = self._get_validation_tasks()

        for r, val_task in enumerate(validation_tasks):

            # initialise list of model iterations (used for visualisation of fine-tuning)
            validation_model_iterations = []

            # make copy of current state of outer model to fine tune for validation
            network_parameters = copy.deepcopy(self.get_params_from_optimiser(self.optimiser_state))

            # sample a task for validation fine-tuning
            validation_x_batch, validation_y_batch = self._generate_batch(tasks=[val_task])

            validation_model_iterations.append(copy.deepcopy(network_parameters))

            # inner loop update
            for _ in range(self.validation_num_inner_updates):

                network_parameters = self.inner_loop_update(network_parameters, validation_x_batch, validation_y_batch)
                
                validation_model_iterations.append(copy.deepcopy(network_parameters))
            
            # sample a new batch from same validation task for testing fine-tuned model
            test_x_batch, test_y_batch = self._generate_batch(tasks=[val_task])

            test_loss = self._compute_loss(network_parameters, test_x_batch, test_y_batch)

            validation_losses.append(float(test_loss))

            if math.isnan(test_loss):
                import pdb; pdb.set_trace()

            if visualise:
                save_name = 'validation_step_{}_rep_{}.png'.format(step_count, r)
                validation_fig = self.visualise(
                    validation_model_iterations, val_task, validation_x_batch, validation_y_batch, save_name=save_name, visualise_all=self.visualise_all
                    )
                validation_figures.append(validation_fig)

        mean_validation_loss = onp.mean(validation_losses)
        var_validation_loss = onp.std(validation_losses)
        
        # get validation loss distribution
        validation_loss_distribution_fig = self._get_validation_loss_distribution_plot(validation_losses)
        # write validation loss distribution figure to tensorboard
        self.writer.add_figure("validation_loss_distribution", validation_loss_distribution_fig, step_count)

        if math.isnan(mean_validation_loss):
            import pdb; pdb.set_trace()

        print('--- validation loss @ step {}: {}'.format(step_count, mean_validation_loss))
        self.writer.add_scalar('meta_metrics/validation_loss_mean', mean_validation_loss, step_count)
        self.writer.add_scalar('meta_metrics/validation_loss_std', var_validation_loss, step_count)

        # get validation loss heatmap as function of parameters governing validation task
        if self.fixed_validation and len(validation_parameter_tuples[0]) == 2:
            validation_loss_heatmap_fig = self._get_validation_loss_heatmap(validation_parameter_tuples, validation_losses)
            self.writer.add_figure("validation_loss_heatmap", validation_loss_heatmap_fig, step_count)
        else:
            warnings.warn("Visualisation of validation losses with parameter space dimension > 2 not supported", Warning)

        if visualise:
            # plot qualitative results
            for f, fig in enumerate(validation_figures):
                self.writer.add_figure("vadliation_plots/repeat_{}".format(f), fig, step_count)

        if self.priority_sample:
            # get figures from priority queue
            priority_queue_fig = self.priority_queue.visualise_priority_queue(feature='losses')
            priority_queue_count_fig = self.priority_queue.visualise_priority_queue(feature='counts')
            priority_queue_loss_dist_fig = self.priority_queue.visualise_priority_queue_loss_distribution()
            
            # write figures from priority queue to tensorboard
            if priority_queue_fig:
                self.writer.add_figure("priority_queue", priority_queue_fig, step_count)
            if priority_queue_count_fig:
                self.writer.add_figure("queue_counts", priority_queue_count_fig, step_count)
            if priority_queue_loss_dist_fig:
                self.writer.add_figure("queue_loss_dist", priority_queue_loss_dist_fig, step_count)

    def fast_validate(self):
        """
        jit accelerated validation loop
        """
        return jit(self.validate)

    def _get_validation_tasks(self):
        """produces set of tasks for use in validation"""
        if self.fixed_validation:
            return self._get_fixed_validation_tasks()
        else:
            return None, self._sample_task(batch_size=self.validation_task_batch_size, validate=True)[0]

    @abstractmethod
    def _get_fixed_validation_tasks(self):
        """
        If using fixed validation this method returns a set of tasks that are 
        'representative' of the task distribution in some meaningful way.
        """
        raise NotImplementedError("Base class method")

    def _get_validation_loss_distribution_plot(self, validation_losses):
        """returns matplotlib figure showing distribution of validation_losses"""
        hist, bin_edges = onp.histogram(validation_losses, bins=int(0.1 * len(validation_losses)))
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

        fig = plt.figure()
        plt.plot(bin_centers, hist)

        return fig

    def _get_validation_loss_heatmap(self, validation_parameter_tuples, validation_losses):
        """returns matplotlib figure showing heatmap of vadliation losses as function of parameter space"""
        unique_parameter_range_lens = []

        for i in range(2):
            unique_parameter_range_lens.append(len(onp.unique([p[i] for p in validation_parameter_tuples])))
        validation_losses_grid = onp.array(validation_losses).reshape(tuple(unique_parameter_range_lens))

        fig = plt.figure()
        plt.imshow(validation_losses_grid)
        plt.colorbar()

        return fig
 