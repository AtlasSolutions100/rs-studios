"""The worker container's entry point (roadmap M7.2; workerproc.py has the details).

    python worker_main.py            run the background loop (docker-compose service "worker")
    python worker_main.py --check    the container healthcheck: exit 0 when the loop beat in the last 180 s
"""
import sys

import workerproc

if __name__ == "__main__":
    sys.exit(workerproc.check() if "--check" in sys.argv[1:] else workerproc.run())
