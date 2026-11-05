"""Own-child process lifecycle on POSIX and native Windows."""

import os
import signal
import subprocess


def group_options():
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def kill_tree(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError, subprocess.SubprocessError):
        if process.poll() is None:
            process.kill()


def stop_worker(process):
    """Give the task runner a chance to terminate its own model/evaluator."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        kill_tree(process)
