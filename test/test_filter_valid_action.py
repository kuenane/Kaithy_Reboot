import sys
sys.path.append('..')

import adversarial_gym as gym
import numpy as np
import tensorflow as tf
import tensorflow.contrib.layers as layers


def opponent_policy(curr_state, prev_state, prev_action):
    '''
    Define policy for opponent here
    '''
    return gym.gym_gomoku.envs.util.make_beginner_policy(np.random)(curr_state, prev_state, prev_action)


def main():
    '''
    AI Self-training program
    '''
    deterministic_filter = True
    random_filter = True

    env = gym.make('Gomoku5x5-training-camp-v0', opponent_policy)

    num_actions = env.action_space.n

    obs_ph = tf.placeholder(
        dtype=tf.float32, shape=[None] + list(env.observation_space.shape))
    q_values = layers.fully_connected(layers.flatten(obs_ph), num_actions)

    if deterministic_filter or random_filter:
        invalid_masks = tf.contrib.layers.flatten(
            tf.reduce_sum(obs_ph[:, :, :, 1:3], axis=3))

    if deterministic_filter:
        q_values_worst = tf.reduce_min(q_values, axis=1, keep_dims=True)
        # q_values = tf.where(tf.equal(
        #     invalid_masks, 1.), q_values_worst - 1.0, q_values)
        q_values = invalid_masks * (q_values_worst - 1.0) + \
            (1.0 - invalid_masks) * q_values

    deterministic_actions = tf.argmax(q_values, axis=1, output_type=tf.int32)
    batch_size = tf.shape(obs_ph)[0]
    stochastic_ph = tf.constant(True, dtype=tf.bool)
    random_actions = tf.random_uniform(
        tf.stack([batch_size]), minval=0, maxval=num_actions, dtype=tf.int32)

    if random_filter:
        def get_elements(data, indices):
            indeces = tf.range(0, tf.shape(indices)[
                0]) * data.shape[1] + indices
            return tf.gather(tf.reshape(data, [-1]), indeces)
        is_invalid_random_actions = get_elements(
            invalid_masks, random_actions)
        random_actions = tf.where(tf.equal(
            is_invalid_random_actions, 1.), deterministic_actions, random_actions)

    chose_random = tf.random_uniform(
        tf.stack([batch_size]), minval=0, maxval=1, dtype=tf.float32) < 0.9
    stochastic_actions = tf.where(
        chose_random, random_actions, deterministic_actions)

    output_actions = tf.where(
        stochastic_ph, stochastic_actions, deterministic_actions)

    optimizer = tf.train.AdamOptimizer(learning_rate=0.001)
    alo = optimizer.minimize(q_values)

    sess = tf.Session()
    sess.run(tf.global_variables_initializer())
    observations = []

    for i in range(2):
        observation = env.reset()
        done = None

        while not done:
            action = sess.run(output_actions, feed_dict={
                obs_ph: observation[None]})[0]
            observation, reward, done, info = env.step(action)
            env.render()
            observations.append(observation)

        print(reward)
        env.swap_role()
        print("\n----SWAP----\n")

    actions = sess.run(output_actions, feed_dict={
        obs_ph: observations})
    sess.run(q_values, feed_dict={
        obs_ph: observations})
    print(actions)


if __name__ == "__main__":
    main()
