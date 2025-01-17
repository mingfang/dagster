import inspect
import sys
import time
from contextlib import contextmanager

import objgraph
from dagster import RunRequest, pipeline, repository, schedule, sensor, solid
from dagster.core.host_representation import ManagedGrpcPythonEnvRepositoryLocationOrigin
from dagster.core.test_utils import instance_for_test
from dagster.core.types.loadable_target_origin import LoadableTargetOrigin
from dagster.daemon.controller import daemon_controller_from_instance


@solid()
def foo_solid(_):
    pass


@pipeline
def foo_pipeline():
    foo_solid()


@pipeline
def other_foo_pipeline():
    foo_solid()


@schedule(
    pipeline_name="foo_pipeline",
    cron_schedule="*/1 * * * *",
)
def always_run_schedule(_context):
    return {}


@sensor(pipeline_name="foo_pipeline", minimum_interval_seconds=10)
def always_on_sensor(_context):
    return RunRequest(run_key=None, run_config={}, tags={})


@repository
def example_repo():
    return [foo_pipeline, always_run_schedule, always_on_sensor]


@contextmanager
def get_example_repository_location():
    loadable_target_origin = LoadableTargetOrigin(
        executable_path=sys.executable,
        python_file=__file__,
    )
    location_name = "example_repo_location"

    origin = ManagedGrpcPythonEnvRepositoryLocationOrigin(loadable_target_origin, location_name)

    with origin.create_test_location() as location:
        yield location


@contextmanager
def get_example_repo():
    with get_example_repository_location() as location:
        yield location.get_repository("example_repo")


def test_no_memory_leaks():
    with instance_for_test(
        overrides={
            "run_coordinator": {
                "module": "dagster.core.run_coordinator",
                "class": "QueuedRunCoordinator",
            },
            "run_launcher": {
                "class": "DefaultRunLauncher",
                "module": "dagster.core.launcher.default_run_launcher",
                "config": {
                    "wait_for_processes": False,
                },
            },
        }
    ) as instance, get_example_repo() as repo:

        external_schedule = repo.get_external_schedule("always_run_schedule")
        external_sensor = repo.get_external_sensor("always_on_sensor")

        instance.start_schedule_and_update_storage_state(external_schedule)
        instance.start_sensor(external_sensor)

        with daemon_controller_from_instance(
            instance, wait_for_processes_on_exit=True
        ) as controller:
            start_time = time.time()

            growth = objgraph.growth(
                limit=10,
                filter=lambda obj: inspect.getmodule(obj)
                and "dagster" in inspect.getmodule(obj).__name__,
            )
            while True:
                time.sleep(45)

                controller.check_daemon_threads()
                controller.check_daemon_heartbeats()

                growth = objgraph.growth(
                    limit=10,
                    filter=lambda obj: inspect.getmodule(obj)
                    and "dagster" in inspect.getmodule(obj).__name__,
                )
                if not growth:
                    print(  # pylint: disable=print-call
                        f"Memory stopped growing after {int(time.time() - start_time)} seconds"
                    )
                    break

                if (time.time() - start_time) > 300:
                    raise Exception(
                        "Memory still growing after 5 minutes. Most recent growth: " + str(growth)
                    )

                print("Growth: " + str(growth))  # pylint: disable=print-call
