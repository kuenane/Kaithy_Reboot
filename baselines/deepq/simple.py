import numpy as np
import os
import dill
import tempfile
import tensorflow as tf
import zipfile
import time
import copy
import sys

import baselines.common.tf_util as U

from baselines import logger
from baselines.common.schedules import LinearSchedule
from baselines import deepq
from baselines.deepq.replay_buffer import ReplayBuffer, PrioritizedReplayBuffer
from baselines.deepq.opponent import Opponent

sys.setrecursionlimit(20000)


class ActWrapper(object):
    def __init__(self, act, act_params):
        self._act = act
        self._act_params = act_params

    @staticmethod
    def load(path, num_cpu=16):
        with open(path, "rb") as f:
            model_data, act_params = dill.load(f)
        act = deepq.build_act(**act_params)
        sess = U.make_session(num_cpu=num_cpu)
        sess.__enter__()
        with tempfile.TemporaryDirectory() as td:
            arc_path = os.path.join(td, "packed.zip")
            with open(arc_path, "wb") as f:
                f.write(model_data)

            zipfile.ZipFile(arc_path, 'r', zipfile.ZIP_DEFLATED).extractall(td)
            U.load_state(os.path.join(td, "model"))

        return ActWrapper(act, act_params)

    def __call__(self, *args, **kwargs):
        return self._act(*args, **kwargs)

    def save(self, path):
        """Save model to a pickle located at `path`"""
        with tempfile.TemporaryDirectory() as td:
            U.save_state(os.path.join(td, "model"))
            arc_name = os.path.join(td, "packed.zip")
            with zipfile.ZipFile(arc_name, 'w') as zipf:
                for root, dirs, files in os.walk(td):
                    for fname in files:
                        file_path = os.path.join(root, fname)
                        if file_path != arc_name:
                            zipf.write(
                                file_path, os.path.relpath(file_path, td))
            with open(arc_name, "rb") as f:
                model_data = f.read()
        with open(path, "wb") as f:
            dill.dump((model_data, self._act_params), f)


def load(path, num_cpu=16):
    """Load act function that was returned by learn function.

    Parameters
    ----------
    path: str
        path to the act function pickle
    num_cpu: int
        number of cpus to use for executing the policy

    Returns
    -------
    act: ActWrapper
        function that takes a batch of observations
        and returns actions.
    """
    return ActWrapper.load(path, num_cpu=num_cpu)


def validate(env, act, kwargs):
    num_episodes = 200
    win_count = 0
    lose_count = 0

    for i in range(num_episodes):
        obs = env.reset()
        while True:
            action = act(obs[None],
                         stochastic=False, **kwargs)[0]
            obs, reward, done, info = env.step(action)
            if done:
                if reward == 1.:
                    win_count += 1
                elif reward == -1.:
                    lose_count += 1
                break
        env.swap_role()

    return win_count, lose_count, num_episodes - win_count - lose_count


def learn(env,
          val_env,
          q_func,
          double_q=True,
          flatten_obs=False,
          lr=5e-4,
          max_timesteps=100000,
          buffer_size=50000,
          exploration_fraction=0.1,
          exploration_final_eps=0.02,
          train_freq=1,
          val_freq=1,
          batch_size=32,
          print_freq=1,
          checkpoint_freq=10000,
          learning_starts=1000,
          gamma=1.0,
          target_network_update_freq=500,
          prioritized_replay=False,
          prioritized_replay_alpha=0.6,
          prioritized_replay_beta0=0.4,
          prioritized_replay_beta_iters=None,
          prioritized_replay_eps=1e-6,
          num_cpu=16,
          param_noise=False,
          callback=None,
          deterministic_filter=False,
          random_filter=False,
          state_file=None):
    """Train a deepq model.

    Parameters
    -------
    env: gym.Env
        environment to train on
    val_env: gym.Env
        environment to valid on
    q_func: (tf.Variable, int, str, bool) -> tf.Variable
        the model that takes the following inputs:
            observation_in: object
                the output of observation placeholder
            num_actions: int
                number of actions
            scope: str
            reuse: bool
                should be passed to outer variable scope
        and returns a tensor of shape (batch_size, num_actions) with values of every action.
    double_q: bool
        if True use target Q to evaluate Q_tp1 (NOTICE: q_func is still used to select a_tp1)
    flatten_obs: bool
        if True flatten obs explicitly
    lr: float
        learning rate for adam optimizer
    max_timesteps: int
        number of env steps to optimizer for
    buffer_size: int
        size of the replay buffer
    exploration_fraction: float
        fraction of entire training period over which the exploration rate is annealed
    exploration_final_eps: float
        final value of random action probability
    train_freq: int
        update the model every `train_freq` steps.
        set to None to disable printing
    val_freq: int
        validate the model every 'val_freq' episodes
    batch_size: int
        size of a batched sampled from replay buffer for training
    print_freq: int
        how often to print out training progress
        set to None to disable printing
    checkpoint_freq: int
        how often to save the model. This is so that the best version is restored
        at the end of the training. If you do not wish to restore the best version at
        the end of the training set this variable to None.
    learning_starts: int
        how many steps of the model to collect transitions for before learning starts
    gamma: float
        discount factor
    target_network_update_freq: int
        update the target network every `target_network_update_freq` steps.
    prioritized_replay: True
        if True prioritized replay buffer will be used.
    prioritized_replay_alpha: float
        alpha parameter for prioritized replay buffer
    prioritized_replay_beta0: float
        initial value of beta for prioritized replay buffer
    prioritized_replay_beta_iters: int
        number of iterations over which beta will be annealed from initial value
        to 1.0. If set to None equals to max_timesteps.
    prioritized_replay_eps: float
        epsilon to add to the TD errors when updating priorities.
    num_cpu: int
        number of cpus to use for training
    callback: (locals, globals) -> None
        function called at every steps with state of the algorithm.
        If callback returns true training stops.

    Returns
    -------
    act: ActWrapper
        Wrapper over act function. Adds ability to save it and load it.
        See header of baselines/deepq/categorical.py for details on the act function.
    """
    # Create all the functions necessary to train the model

    sess = U.make_session(num_cpu=num_cpu)
    sess.__enter__()

    def make_obs_ph(name):
        obs_shape = env.observation_space.shape

        # if flatten_obs:
        #     flattened_env_shape = 1
        #     for dim_size in env.observation_space.shape:
        #         flattened_env_shape *= dim_size
        #     obs_shape = (flattened_env_shape,)

        return U.BatchInput(obs_shape, name=name)

    act, train, update_target, debug = deepq.build_train(
        make_obs_ph=make_obs_ph,
        q_func=q_func,
        num_actions=env.action_space.n,
        optimizer=tf.train.AdamOptimizer(learning_rate=lr),
        gamma=gamma,
        grad_norm_clipping=10,
        double_q=double_q,
        param_noise=param_noise,
        deterministic_filter=deterministic_filter,
        random_filter=random_filter
    )

    act_params = {
        'make_obs_ph': make_obs_ph,
        'q_func': q_func,
        'num_actions': env.action_space.n,
        'random_filter': random_filter,
        'deterministic_filter': deterministic_filter,
    }

    # Create the replay buffer
    if prioritized_replay:
        replay_buffer = PrioritizedReplayBuffer(
            buffer_size, alpha=prioritized_replay_alpha)
        if prioritized_replay_beta_iters is None:
            prioritized_replay_beta_iters = max_timesteps
        beta_schedule = LinearSchedule(prioritized_replay_beta_iters,
                                       initial_p=prioritized_replay_beta0,
                                       final_p=1.0)
    else:
        replay_buffer = ReplayBuffer(buffer_size)
        beta_schedule = None
    # Create the schedule for exploration starting from 1.
    exploration = LinearSchedule(schedule_timesteps=int(exploration_fraction * max_timesteps),
                                 initial_p=1.0,
                                 final_p=exploration_final_eps)

    # Initialize the parameters and copy them to the target network.
    U.initialize()

    if state_file is not None:
        try:
            with open(state_file, "rb") as f:
                model_data, act_params = dill.load(f)
            with tempfile.TemporaryDirectory() as td:
                arc_path = os.path.join(td, "packed.zip")
                with open(arc_path, "wb") as f:
                    f.write(model_data)

                zipfile.ZipFile(
                    arc_path, 'r', zipfile.ZIP_DEFLATED).extractall(td)
                U.load_state(os.path.join(td, "model"))
            print('Saved model is loaded, training is resume')
        except FileNotFoundError as e:
            print('No model to loaded, training start from scratch')

    update_target()

    episode_rewards = [0.0]
    saved_mean_reward = None
    saved_num_win = 1
    saved_time_step = None

    opponent = Opponent(flatten_obs=flatten_obs, act=act,
                        replay_buffer=replay_buffer)
    env.opponent_policy = opponent.policy

    obs = env.reset()
    reset = True
    start_time = time.time()
    start_clock = time.clock()
    total_error = None

    with tempfile.TemporaryDirectory() as td:
        model_saved = False
        model_file = os.path.join(td, "model")
        for t in range(max_timesteps):
            if callback is not None:
                if callback(locals(), globals()):
                    break
            # Take action and update exploration to the newest value
            kwargs = {}
            if not param_noise:
                update_eps = exploration.value(t)
                update_param_noise_threshold = 0.
            else:
                update_eps = 0.
                # Compute the threshold such that the KL divergence between perturbed and non-perturbed
                # policy is comparable to eps-greedy exploration with eps = exploration.value(t).
                # See Appendix C.1 in Parameter Space Noise for Exploration, Plappert et al., 2017
                # for detailed explanation.
                update_param_noise_threshold = - \
                    np.log(1. - exploration.value(t) +
                           exploration.value(t) / float(env.action_space.n))
                kwargs['reset'] = reset
                kwargs['update_param_noise_threshold'] = update_param_noise_threshold
                kwargs['update_param_noise_scale'] = True
            # if flatten_obs:
            #     obs = obs.flatten()
            action = act(np.array(obs)[None],
                         update_eps=update_eps, **kwargs)[0]
            reset = False
            new_obs, rew, done, _ = env.step(action)
            # if flatten_obs:
            #     new_obs = new_obs.flatten()
            # Store transition in the replay buffer.

            episode_rewards[-1] += rew
            if done:
                # Player is black
                new_obs[:, :, 0] = 0
                replay_buffer.add(obs, action, rew, new_obs, float(done))

                # Opponent is white
                new_obs[:, :, 0] = 1
                if opponent.old_obs is not None:
                    replay_buffer.add(opponent.old_obs,
                                      opponent.old_action, -rew, new_obs, float(done))
                obs = env.reset()
                opponent.reset()

                episode_rewards.append(0.0)
                reset = True
            else:
                replay_buffer.add(obs, action, rew, new_obs, float(done))
                obs = new_obs

            if t > learning_starts and t % train_freq == 0:
                # Minimize the error in Bellman's equation on a batch sampled from replay buffer.
                if prioritized_replay:
                    experience = replay_buffer.sample(
                        batch_size, beta=beta_schedule.value(t))
                    (obses_t, actions, rewards, obses_tp1,
                     dones, weights, batch_idxes) = experience
                else:
                    obses_t, actions, rewards, obses_tp1, dones = replay_buffer.sample(
                        batch_size)
                    weights, batch_idxes = np.ones_like(rewards), None
                td_errors, base_error, total_error = train(obses_t, actions, rewards,
                                                           obses_tp1, dones, weights)
                if prioritized_replay:
                    new_priorities = np.abs(td_errors) + prioritized_replay_eps
                    replay_buffer.update_priorities(
                        batch_idxes, new_priorities)

            if t > learning_starts and t % target_network_update_freq == 0:
                # Update target network periodically.
                update_target()

            mean_100ep_reward = round(np.mean(episode_rewards[-101:-1]), 1)
            num_episodes = len(episode_rewards)
            if done and print_freq is not None and len(episode_rewards) % print_freq == 0:
                logger.record_tabular(
                    "Execution time", time.time() - start_time)
                logger.record_tabular(
                    "Wall-clock time", time.clock() - start_clock)
                logger.record_tabular("steps", t)
                if total_error is not None:
                    logger.record_tabular(
                        "Base error", base_error)
                    logger.record_tabular(
                        "Total error", total_error)
                logger.record_tabular("episodes", num_episodes)
                logger.record_tabular(
                    "mean 100 episode reward", mean_100ep_reward)
                logger.record_tabular(
                    "% time spent exploring", int(100 * exploration.value(t)))
                logger.dump_tabular()
                start_time = time.time()
                start_clock = time.clock()

            if done and val_env is not None and val_freq is not None and len(episode_rewards) % val_freq == 0:
                num_win, num_lose, num_draw = validate(val_env, act, kwargs)
                if print_freq is not None:
                    logger.record_tabular(
                        "Execution time", time.time() - start_time)
                    logger.record_tabular(
                        "Wall-clock time", time.clock() - start_clock)
                    logger.record_tabular("win", num_win)
                    logger.record_tabular("lose", num_lose)
                    logger.record_tabular("draw", num_draw)
                    logger.dump_tabular()
                    start_time = time.time()
                    start_clock = time.clock()

                if (num_win >= saved_num_win):
                    logger.log("Saving model due to num win increase or same as before: {} -> {}".format(
                        saved_num_win, num_win))
                    U.save_state(model_file)
                    model_saved = True
                    saved_time_step = t
                    saved_num_win = num_win
                    saved_num_lose = num_lose
                elif saved_time_step is not None:
                    logger.log("Nothing improve keep saved state at time step {} with num win-lose: {}-{}".format(
                        saved_time_step, saved_num_win, saved_num_lose))
                else:
                    logger.log(
                        "Nothing improve keep saved state at time step 0")
                    # if (checkpoint_freq is not None and t > learning_starts and
                    #         num_episodes > 100 and t % checkpoint_freq == 0):
                    #     if saved_mean_reward is None or mean_100ep_reward > saved_mean_reward:
                    #         if print_freq is not None:
                    #             logger.log("Saving model due to mean reward increase: {} -> {}".format(
                    #                        saved_mean_reward, mean_100ep_reward))
                    #         U.save_state(model_file)
                    #         model_saved = True
                    #         saved_mean_reward = mean_100ep_reward
        if model_saved:
            if print_freq is not None:
                logger.log("Restored model at time step {} with num win-lose: {}-{}".format(
                    saved_time_step, saved_num_win, saved_num_lose))
            U.load_state(model_file)

    return ActWrapper(act, act_params)
