import tensorflow as tf


def main():
    input = tf.constant([
        [
            [[1, 0, 0], [0, 1, 0], [0, 1, 0]],
            [[0, 0, 0], [0, 0, 0], [0, 0, 0]],
            [[0, 0, 0], [0, 1, 0], [0, 0, 0]]
        ],
        [
            [[1, 0, 1], [0, 1, 1], [0, 1, 1]],
            [[0, 0, 1], [0, 0, 1], [0, 0, 1]],
            [[1, 0, 1], [0, 0, 1], [0, 0, 1]]
        ],
    ], dtype=tf.float32)
    output = tf.contrib.layers.flatten(
        tf.reduce_sum(input[:, :, :, 0:2], axis=3))
    sess = tf.Session()
    print(sess.run(output))


if __name__ == "__main__":
    main()