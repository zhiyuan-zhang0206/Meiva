# meiva:note
# A session leader that owns the slave side of a pty, so the master a test holds has a real foreground group.
#
# The test hands over the slave descriptor number and waits for the line "ready". The terminal's
# foreground group is then this process's own group, in a session this process leads.
import fcntl
import sys
import termios
import time


def main(slave_fd: int) -> None:
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    time.sleep(300)


if __name__ == "__main__":
    main(int(sys.argv[1]))
