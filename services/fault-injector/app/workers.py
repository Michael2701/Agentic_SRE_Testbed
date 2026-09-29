"""Resource hogs run *inside* target containers via docker exec, so they compete for the container's real
CPU quota / memory limit. The images' own python is used; each process writes a pidfile so it can be checked
and killed. Neutral names: nothing inside the container says "fault".
"""

from app.docker_api import Docker

_CPU = "import os,sys\nopen(sys.argv[1],'w').write(str(os.getpid()))\nwhile True: pass"
_MEMORY = (
    "import os,sys,time\nopen(sys.argv[1],'w').write(str(os.getpid()))\n"
    "b=bytearray(int(sys.argv[2])*1048576)\n"
    "for i in range(0,len(b),4096): b[i]=1\n"  # touch every page so it is resident, not just reserved
    "time.sleep(10**9)"
)


def _glob(fault_id: str) -> str:
    return f"/tmp/.w-{fault_id}-*.pid"


class Workers:
    def __init__(self, docker: Docker):
        self.docker = docker

    async def running(self, service: str, fault_id: str) -> bool:
        check = (f'set -- {_glob(fault_id)}; [ -f "$1" ] || exit 1; '
                 f'for f in "$@"; do kill -0 "$(cat "$f")" 2>/dev/null || exit 1; done')
        return await self.docker.exec(service, ["sh", "-c", check]) == 0

    async def kill(self, service: str, fault_id: str) -> None:
        script = f'for f in {_glob(fault_id)}; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; done; true'
        await self.docker.exec(service, ["sh", "-c", script])

    async def ensure(self, fault: dict) -> None:
        """Idempotent: (re)starts the hogs if any is missing, e.g. after a container restart or an OOM kill."""
        service, fault_id, params = fault["target"], fault["id"], fault["parameters"]
        if await self.running(service, fault_id):
            return
        await self.kill(service, fault_id)
        if fault["type"] == "cpu_saturation":
            for i in range(params["workers"]):
                await self.docker.exec(service, ["python", "-c", _CPU, f"/tmp/.w-{fault_id}-{i}.pid"], wait=False)
        else:
            await self.docker.exec(
                service, ["python", "-c", _MEMORY, f"/tmp/.w-{fault_id}-0.pid", str(params["mb"])], wait=False
            )
