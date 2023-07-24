import asyncio
import math
import os
import time
from typing import Optional

from aiodocker import DockerError
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from ataka.common import database
from ataka.common.database.models import Job, Execution
from ataka.common.queue import get_channel, JobQueue, JobAction
from .localdata import *
from ..common.queue.output import OutputQueue, OutputMessage


class BuildError(Exception):
    pass


class Jobs:
    def __init__(self, docker, exploits):
        self._docker = docker
        self._exploits = exploits
        self._jobs = {}

    async def poll_and_run_jobs(self):
        async with await get_channel() as channel:
            job_queue = await JobQueue.get(channel)

            async for job_message in job_queue.wait_for_messages():
                match job_message.action:
                    case JobAction.CANCEL:
                        print(f"DEBUG: CURRENTLY RUNNING {len(self._jobs)}")
                        result = [(task, job) for task, job in self._jobs.items() if job.id == job_message.job_id]
                        if len(result) > 0:
                            task, job = result[0]
                            await task.cancel()
                    case JobAction.QUEUE:
                        job_execution = JobExecution(self._docker, self._exploits, channel, job_message.job_id)
                        task = asyncio.create_task(job_execution.run())

                        def on_done(job):
                            del self._jobs[job]

                        self._jobs[task] = job_execution
                        task.add_done_callback(on_done)


class JobExecution:
    def __init__(self, docker, exploits, channel, job_id: int):
        self.id = job_id
        self._docker = docker
        self._exploits = exploits
        self._channel = channel
        self._data_store = os.environ["DATA_STORE"]

    async def run(self):
        job = await self.fetch_job_from_database()
        if job is None:
            return

        exploit = job.exploit

        persist_dir = f"/data/persist/{exploit.file}"
        host_persist_dir = f"{self._data_store}/persist/{exploit.file}"

        try:
            os.makedirs(persist_dir, exist_ok=True)
            container_ref = await self._docker.containers.create_or_replace(name=f"ataka-exploit-{exploit.file}",
                                                                            config={
                                                                                "Image": exploit.docker_id,
                                                                                "Cmd": ["sleep", str(math.floor(
                                                                                    job.timeout - time.time()))],
                                                                                "AttachStdin": False,
                                                                                "AttachStdout": False,
                                                                                "AttachStderr": False,
                                                                                "Tty": False,
                                                                                "OpenStdin": False,
                                                                                "StopSignal": "SIGKILL",
                                                                                "HostConfig": {
                                                                                    "Mounts": [{
                                                                                        "Type": "bind",
                                                                                        "Source": host_persist_dir,
                                                                                        "Target": "/persist",
                                                                                    }]
                                                                                }
                                                                            })

            await container_ref.start()
        except DockerError as exception:
            print(f"Got docker error {exception}")
            print(f"   {exploit}")
            for e in job.executions:
                e.status = JobExecutionStatus.FAILED
                e.stderr = str(exception)
            await self.submit_to_database(job.executions)
            raise exception

        execute_tasks = [self.docker_execute(container_ref, e) for e in job.executions]

        # Execute all the exploits
        results = await asyncio.gather(*execute_tasks)

        #try:
        #    os.rmdir(persist_dir)
        #except (FileNotFoundError, OSError):
        #    pass

        await self.submit_to_database(results)
        # TODO: send to ctfconfig

    async def fetch_job_from_database(self) -> Optional[LocalJob]:
        async with database.get_session() as session:
            get_job = select(Job).where(Job.id == self.id)
            job = (await session.execute(get_job)).scalar_one()
            get_executions = select(Execution).where(Execution.job_id == self.id) \
                .options(selectinload(Execution.target))
            executions = (await session.execute(get_executions)).scalars()

            if job.timeout.timestamp() - time.time() < 0:
                job.status = JobExecutionStatus.TIMEOUT
                for e in executions:
                    e.status = JobExecutionStatus.TIMEOUT
                    e.stderr = "<EXECUTOR TIMEOUT HAPPENED>"
                await session.commit()
                return None

            exploit = await self._exploits.ensure_exploit(job.exploit_id)
            if exploit.status is not LocalExploitStatus.FINISHED:
                print(f"Got error exploit {exploit.build_output}")
                print(f"   {exploit}")
                job.status = JobExecutionStatus.FAILED
                for e in executions:
                    e.status = JobExecutionStatus.FAILED
                    e.stderr = exploit.build_output
                await session.commit()
                return None

            job.status = JobExecutionStatus.RUNNING
            local_executions = []
            for e in executions:
                e.status = JobExecutionStatus.RUNNING
                local_executions += [
                    LocalExecution(e.id, exploit, LocalTarget(e.target.ip, e.target.extra), JobExecutionStatus.RUNNING)]

            await session.commit()

            # Convert data to local for usage without database
            return LocalJob(exploit, job.timeout.timestamp(), local_executions)

    async def submit_to_database(self, results: [LocalExecution]):
        local_executions = {e.database_id: e for e in results}
        status = JobExecutionStatus.FAILED if any([e.status == JobExecutionStatus.FAILED for e in results]) \
            else JobExecutionStatus.CANCELLED if any([e.status == JobExecutionStatus.CANCELLED for e in results]) \
            else JobExecutionStatus.FINISHED

        # submit results to database
        async with database.get_session() as session:
            get_job = select(Job).where(Job.id == self.id)
            job = (await session.execute(get_job)).scalar_one()
            job.status = status

            get_executions = select(Execution).where(Execution.job_id == self.id) \
                .options(selectinload(Execution.target))
            executions = (await session.execute(get_executions)).scalars()

            for execution in executions:
                local_execution = local_executions[execution.id]
                execution.status = local_execution.status
                execution.stdout = local_execution.stdout
                execution.stderr = local_execution.stderr

            await session.commit()

    async def docker_execute(self, container_ref, execution: LocalExecution) -> LocalExecution:
        async def exec_in_container_and_poll_output():
            try:
                exec_ref = await container_ref.exec(cmd=execution.exploit.docker_cmd, workdir="/exploit", tty=False, environment={
                    "TARGET_IP": execution.target.ip,
                    "TARGET_EXTRA": execution.target.extra,
                })
                async with exec_ref.start(detach=False) as stream:
                    while True:
                        message = await stream.read_out()
                        if message is None:
                            break

                        yield message[0], message[1].decode()
            except DockerError as e:
                msg = f"DOCKER EXECUTION ERROR: {e.message}"
                execution.status = JobExecutionStatus.FAILED
                execution.stderr += msg
                yield 2, msg


        output_queue = await OutputQueue.get(self._channel)

        async for (stream, output) in exec_in_container_and_poll_output():
            # collect output
            match stream:
                case 1:
                    execution.stdout += output
                case 2:
                    execution.stderr += output

            await output_queue.send_message(OutputMessage(None, execution.database_id, stream == 1, output))
        if execution.status in [JobExecutionStatus.QUEUED, JobExecutionStatus.RUNNING]:
            execution.status = JobExecutionStatus.FINISHED
        return execution
