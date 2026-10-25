import asyncio
import importlib.util
import json

import pytest

import lte_pro_access
from lte_pro_access import __main__ as main_mod
from lte_pro_access import service as service_mod
from lte_pro_access.config import load_config
from lte_pro_access.device import AsyncSshDevice
from lte_pro_access.ssh_enable import enable_ssh_via_unifi
from lte_pro_access.supervisor import TICK_TIMEOUT_SECONDS, Supervisor
from test_relays import SAMPLE


def present(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())


def missing(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(SAMPLE))
    return path


def test_missing_pyunifiapi_raises_a_clear_error(monkeypatch):
    missing(monkeypatch)
    with pytest.raises(ModuleNotFoundError) as exc:
        service_mod.require_pyunifiapi()
    assert exc.value.name == "pyunifiapi"
    assert "README" in str(exc.value)


def test_present_pyunifiapi_passes(monkeypatch):
    seen = []

    def find_spec(name):
        seen.append(name)
        return object()

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    service_mod.require_pyunifiapi()
    assert seen == ["pyunifiapi"]


def test_create_supervisor_wires_the_real_dependencies(monkeypatch, config_path):
    present(monkeypatch)
    cfg = load_config(config_path)
    built = []

    def fake_supervisor(*args):
        built.append(args)
        return "supervisor"

    monkeypatch.setattr(service_mod, "Supervisor", fake_supervisor)
    assert service_mod.create_supervisor(cfg) == "supervisor"
    [(got_cfg, device, enable_ssh, spawn_local, sleep)] = built
    assert got_cfg is cfg
    assert isinstance(device, AsyncSshDevice)
    assert enable_ssh is enable_ssh_via_unifi
    assert spawn_local is service_mod.spawn_local
    assert sleep is asyncio.sleep


def test_create_supervisor_returns_a_supervisor(monkeypatch, config_path):
    present(monkeypatch)
    assert isinstance(service_mod.create_supervisor(load_config(config_path)), Supervisor)


def test_create_supervisor_fails_without_pyunifiapi(monkeypatch, config_path):
    missing(monkeypatch)
    with pytest.raises(ModuleNotFoundError):
        service_mod.create_supervisor(load_config(config_path))


async def test_spawn_local_runs_the_argv_with_no_stdio():
    proc = await service_mod.spawn_local(["true"])
    assert await proc.wait() == 0


async def test_amain_exits_before_loading_config_or_starting_anything_when_pyunifiapi_is_missing(
    monkeypatch, config_path
):
    missing(monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("must not start the supervisor")

    monkeypatch.setattr(main_mod, "load_config", forbidden)
    monkeypatch.setattr(main_mod, "create_supervisor", forbidden)
    monkeypatch.setattr(main_mod, "run_forever", forbidden)
    with pytest.raises(SystemExit) as exc:
        await main_mod.amain(config_path)
    assert "pyunifiapi" in str(exc.value)


async def test_amain_runs_the_helper_supervisor_and_closes_it(monkeypatch, config_path):
    present(monkeypatch)
    created = []
    runs = []

    class Stop(Exception):
        pass

    class FakeSupervisor:
        closed = False

        def close(self):
            self.closed = True

    def create(cfg):
        created.append(cfg)
        return FakeSupervisor()

    async def run_forever(supervisor, interval, tick_timeout, sleep):
        runs.append((supervisor, interval, tick_timeout, sleep))
        raise Stop

    monkeypatch.setattr(main_mod, "create_supervisor", create)
    monkeypatch.setattr(main_mod, "run_forever", run_forever)
    with pytest.raises(Stop):
        await main_mod.amain(config_path)
    assert created == [load_config(config_path)]
    supervisor, interval, tick_timeout, sleep = runs[0]
    assert interval == SAMPLE["check_interval_seconds"]
    assert tick_timeout == TICK_TIMEOUT_SECONDS
    assert sleep is asyncio.sleep
    assert supervisor.closed


def test_public_api():
    assert sorted(lte_pro_access.__all__) == sorted(
        [
            "TICK_TIMEOUT_SECONDS",
            "AtRelayConfig",
            "Config",
            "Supervisor",
            "TickResult",
            "create_supervisor",
            "run_forever",
        ]
    )
    for name in lte_pro_access.__all__:
        assert hasattr(lte_pro_access, name)
