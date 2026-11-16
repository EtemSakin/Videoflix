"""Run the web server and RQ worker as supervised child processes."""

import signal
import subprocess
import sys
import time

WORKER_COMMAND = ["python", "manage.py", "rqworker", "default"]


def stop_supervisor(_signum, _frame):
    """Stop cleanly when Docker sends its termination signal."""
    raise SystemExit(0)


def first_exit_code(processes):
    """Return the exit code of the first service process that stopped."""
    for process in processes:
        return_code = process.poll()
        if return_code is not None:
            return return_code
    return 1


def terminate_processes(processes):
    """Stop child processes, then force termination after a grace period."""
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_services(web_command):
    """Run both required services and exit when either service stops."""
    signal.signal(signal.SIGTERM, stop_supervisor)
    processes = []
    try:
        processes.append(subprocess.Popen(WORKER_COMMAND))
        processes.append(subprocess.Popen(web_command))
        while all(process.poll() is None for process in processes):
            time.sleep(1)
        return first_exit_code(processes)
    finally:
        terminate_processes(processes)


if __name__ == "__main__":
    sys.exit(run_services(sys.argv[1:]))
