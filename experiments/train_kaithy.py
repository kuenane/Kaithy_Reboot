import sys
sys.path.append('..')

from template.gomoku import train


def main():
    try:
        train(int(sys.argv[1]), int(sys.argv[2]))
    except Exception as e:
        print('Usage:')
        print('\tcd ./experiments')
        print('\tpython ./train_kaithy board_size max_time_step')


if __name__ == '__main__':
    main()