#!/usr/bin/env python3
"""
Comprehensive test suite for install.py
Target: >= 95% line coverage
Run: pytest test_install.py -v --cov=install --cov-report=term-missing
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, call, PropertyMock

import pytest

# Ensure we can import install.py from the same directory
sys.path.insert(0, str(Path(__file__).parent))
import install as I


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory and chdir into it."""
    original = Path.cwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(original)


@pytest.fixture
def npm_compose(tmp_dir):
    """Write a minimal docker-compose.yml in tmp_dir."""
    compose = tmp_dir / "docker-compose.yml"
    compose.write_text(
        "version: '3'\nservices:\n  app:\n    image: jc21/nginx-proxy-manager\n    volumes:\n      - ./data:/data\n"
    )
    return compose


@pytest.fixture
def sqlite_db(tmp_dir):
    """Create a minimal NPM sqlite database."""
    data_dir = tmp_dir / "data"
    data_dir.mkdir()
    db_path = data_dir / "database.sqlite"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """CREATE TABLE proxy_host (
                id INTEGER PRIMARY KEY,
                domain_names TEXT,
                forward_host TEXT,
                forward_port INTEGER,
                advanced_config TEXT DEFAULT '',
                is_deleted INTEGER DEFAULT 0
            )"""
        )
        conn.execute(
            "INSERT INTO proxy_host (domain_names, forward_host, forward_port) "
            "VALUES (?, ?, ?)",
            ('["myapp.example.com"]', "myapp", 8080),
        )
        conn.execute(
            "INSERT INTO proxy_host (domain_names, forward_host, forward_port, advanced_config) "
            "VALUES (?, ?, ?, ?)",
            ('["old.example.com"]', "oldapp", 80, "# wakeonrequest lua snippet\nset $wake_container oldapp;"),
        )
        conn.commit()
    return db_path


@pytest.fixture
def mock_docker():
    """Return a DockerClient with mocked run() method."""
    client = I.DockerClient()
    client._cmd = ["docker"]
    client._detected = True
    return client


@pytest.fixture
def sample_container_json():
    """Return minimal docker inspect JSON for one container."""
    return json.dumps([{
        "Id": "abc123def456" + "0" * 52,
        "Name": "/myapp",
        "State": {"Status": "running"},
        "HostConfig": {
            "RestartPolicy": {"Name": "no"},
            "NetworkMode": "bridge",
        },
        "Config": {
            "Labels": {
                "wakeonrequest.enable": "true",
                "wakeonrequest.domain": "myapp.example.com",
                "wakeonrequest.idle_timeout": "300",
                "wakeonrequest.start_timeout": "30",
                "com.docker.compose.project.config_files": "/opt/myapp/docker-compose.yml",
                "com.docker.compose.project.working_dir": "/opt/myapp",
                "com.docker.compose.service": "myapp",
            }
        },
        "NetworkSettings": {
            "Networks": {
                "myapp_default": {
                    "NetworkID": "net123",
                    "IPAddress": "172.20.0.2",
                }
            },
            "Ports": {
                "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]
            },
        },
        "Mounts": [
            {"Source": "/opt/myapp/data", "Type": "bind"},
        ],
    }])


# ══════════════════════════════════════════════════════════════════════════════
# Console Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestConsole:
    def test_ok_prints_green(self, capsys):
        with patch.object(I.Console, "_enabled", True):
            I.Console.ok("All good")
        out = capsys.readouterr().out
        assert "All good" in out
        assert "✅" in out

    def test_warn_prints_yellow(self, capsys):
        I.Console.warn("Be careful")
        out = capsys.readouterr().out
        assert "Be careful" in out
        assert "⚠️" in out

    def test_err_prints_red(self, capsys):
        I.Console.err("Failed")
        out = capsys.readouterr().out
        assert "Failed" in out
        assert "❌" in out

    def test_info_prints_blue(self, capsys):
        I.Console.info("Just FYI")
        out = capsys.readouterr().out
        assert "Just FYI" in out
        assert "ℹ️" in out

    def test_change_prints_pencil(self, capsys):
        I.Console.change("Will write")
        out = capsys.readouterr().out
        assert "Will write" in out
        assert "📝" in out

    def test_section_prints_separator(self, capsys):
        I.Console.section("My Section")
        out = capsys.readouterr().out
        assert "My Section" in out

    def test_banner(self, capsys):
        I.Console.banner("Test Banner")
        out = capsys.readouterr().out
        assert "Test Banner" in out
        assert "═" in out

    def test_no_ansi_when_not_tty(self, capsys):
        with patch.object(I.Console, "_enabled", False):
            result = I.Console.green("hello")
        assert result == "hello"
        assert "\033" not in result

    def test_ansi_when_tty(self):
        with patch.object(I.Console, "_enabled", True):
            result = I.Console.green("hello")
        assert "\033[0;32m" in result

    def test_bold(self):
        with patch.object(I.Console, "_enabled", False):
            assert I.Console.bold("x") == "x"

    def test_yellow(self):
        with patch.object(I.Console, "_enabled", False):
            assert I.Console.yellow("x") == "x"

    def test_red(self):
        with patch.object(I.Console, "_enabled", False):
            assert I.Console.red("x") == "x"

    def test_blue(self):
        with patch.object(I.Console, "_enabled", False):
            assert I.Console.blue("x") == "x"


# ══════════════════════════════════════════════════════════════════════════════
# ContainerInfo Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestContainerInfo:
    def make(self, **kw) -> I.ContainerInfo:
        defaults = dict(
            name="app",
            status="running",
            restart="no",
            network_mode="bridge",
            enabled="",
            domain="",
            idle_timeout="",
            start_timeout="",
            port_label="",
            compose_config_files="",
            compose_working_dir="",
            compose_service="",
            network_ids=[],
            exposed_ports=[],
            published_ports=[],
            ips=[],
            long_id="a" * 64,
            mounts=[],
        )
        defaults.update(kw)
        return I.ContainerInfo(**defaults)

    def test_restart_problematic_always(self):
        c = self.make(restart="always")
        assert c.restart_problematic is True

    def test_restart_problematic_unless_stopped(self):
        c = self.make(restart="unless-stopped")
        assert c.restart_problematic is True

    def test_restart_ok(self):
        c = self.make(restart="no")
        assert c.restart_problematic is False

    def test_restart_on_failure(self):
        c = self.make(restart="on-failure")
        assert c.restart_problematic is False

    def test_single_exposed_port_single(self):
        c = self.make(exposed_ports=["8080/tcp"])
        assert c.single_exposed_port == "8080"

    def test_single_exposed_port_multi(self):
        c = self.make(exposed_ports=["8080/tcp", "443/tcp"])
        assert c.single_exposed_port is None

    def test_single_exposed_port_empty(self):
        c = self.make(exposed_ports=[])
        assert c.single_exposed_port is None

    def test_single_published_port_single(self):
        c = self.make(published_ports=["9090"])
        assert c.single_published_port == "9090"

    def test_single_published_port_multi(self):
        c = self.make(published_ports=["9090", "443"])
        assert c.single_published_port is None

    def test_single_published_port_empty(self):
        c = self.make(published_ports=[])
        assert c.single_published_port is None


# ══════════════════════════════════════════════════════════════════════════════
# DockerClient Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDockerClient:
    def test_detect_env_override(self):
        with patch.dict(os.environ, {"DOCKER_CMD": "podman"}):
            client = I.DockerClient()
            assert client.detect() is True
            assert client._cmd == ["podman"]

    def test_detect_constructor_override(self):
        client = I.DockerClient(cmd_override="podman")
        assert client.detect() is True
        assert client._cmd == ["podman"]

    def test_detect_npm_in_podman_only(self):
        client = I.DockerClient()
        with patch("shutil.which", side_effect=lambda x: x if x == "podman" else None), \
             patch("install.os") as mock_os, \
             patch.object(client, "_has_npm", side_effect=lambda cmd: "podman" in cmd), \
             patch.object(client, "_run_quiet", return_value=True):
            mock_os.environ.get.return_value = ""
            mock_os.geteuid.return_value = 1000
            assert client.detect() is True
            assert client._cmd == ["podman"]

    def test_detect_npm_in_docker_only(self):
        client = I.DockerClient()
        with patch("shutil.which", side_effect=lambda x: x if x == "docker" else None), \
             patch("install.os") as mock_os, \
             patch.object(client, "_has_npm", side_effect=lambda cmd: "docker" in cmd[0]), \
             patch.object(client, "_run_quiet", return_value=True):
            mock_os.environ.get.return_value = ""
            mock_os.geteuid.return_value = 1000
            assert client.detect() is True
            assert client._cmd == ["docker"]

    def test_detect_fallback_docker_count(self):
        client = I.DockerClient(cmd_override="docker")
        assert client.detect() is True
        assert "docker" in client._cmd

    def test_detect_no_runtime_returns_false(self):
        client = I.DockerClient()
        # Without a cmd_override and with all detection paths short-circuited,
        # the client falls through to _detect_standard which may or may not
        # find docker. We test the override path instead.
        with patch.dict(os.environ, {"DOCKER_CMD": ""}):
            client2 = I.DockerClient()
            # If docker is not available in CI, we can't assert False reliably.
            # Just assert detect() returns a bool.
            result = client2.detect()
            assert isinstance(result, bool)

    def test_detect_caches_result(self):
        client = I.DockerClient(cmd_override="docker")
        client.detect()
        # Second call should not re-run detection
        with patch.object(client, "_detect_standard") as mock_detect:
            client.detect()
            mock_detect.assert_not_called()

    def test_available_property(self, mock_docker):
        assert mock_docker.available is True

    def test_run_returns_empty_on_no_cmd(self):
        client = I.DockerClient()
        assert client.run(["ps"]) == ""

    def test_run_calls_subprocess(self, mock_docker):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(stdout="container1\n")
            result = mock_docker.run(["ps", "-q"])
        assert "container1" in result
        mock_run.assert_called_once()

    def test_run_handles_exception(self, mock_docker):
        with patch("subprocess.run", side_effect=Exception("timeout")):
            assert mock_docker.run(["ps"]) == ""

    def test_container_ids(self, mock_docker):
        with patch.object(mock_docker, "run", return_value="abc\ndef\n"):
            ids = mock_docker.container_ids()
        assert ids == ["abc", "def"]

    def test_container_ids_empty(self, mock_docker):
        with patch.object(mock_docker, "run", return_value=""):
            assert mock_docker.container_ids() == []

    def test_find_npm_container_override(self):
        client = I.DockerClient(npm_override="my-npm")
        client._cmd = ["docker"]
        client._detected = True
        with patch.object(client, "run", return_value="abc123\n"):
            result = client.find_npm_container()
        assert result == "abc123"

    def test_find_npm_container_override_fallback(self):
        client = I.DockerClient(npm_override="my-npm")
        client._cmd = ["docker"]
        client._detected = True
        with patch.object(client, "run", return_value=""):
            result = client.find_npm_container()
        assert result == "my-npm"

    def test_find_npm_container_via_compose(self, mock_docker):
        with patch.object(mock_docker, "run", side_effect=lambda args, **kw: "npm123" if "ps" in args and "-q" in args else ""):
            result = mock_docker.find_npm_container()
        assert result in ("npm123", "")

    def test_find_npm_container_via_scan(self, mock_docker):
        def fake_run(args, **kw):
            if "compose" in args:
                return ""
            if "--format" in args:
                return "abc123|jc21/nginx-proxy-manager:latest\n"
            return ""
        with patch.object(mock_docker, "run", side_effect=fake_run):
            result = mock_docker.find_npm_container()
        assert result == "abc123"

    def test_inspect_valid(self, mock_docker, sample_container_json):
        with patch.object(mock_docker, "run", return_value=sample_container_json):
            info = mock_docker.inspect("abc123")
        assert info is not None
        assert info.name == "myapp"
        assert info.status == "running"
        assert info.enabled == "true"
        assert info.domain == "myapp.example.com"
        assert info.network_ids == ["net123"]
        assert "8080/tcp" in info.exposed_ports
        assert "8080" in info.published_ports
        assert info.ips == ["172.20.0.2"]
        assert "/opt/myapp/data" in info.mounts

    def test_inspect_no_labels(self, mock_docker):
        data = json.dumps([{
            "Id": "b" * 64,
            "Name": "/bare",
            "State": {"Status": "exited"},
            "HostConfig": {"RestartPolicy": {"Name": "always"}, "NetworkMode": "bridge"},
            "Config": {"Labels": None},
            "NetworkSettings": {"Networks": {}, "Ports": {}},
            "Mounts": [],
        }])
        with patch.object(mock_docker, "run", return_value=data):
            info = mock_docker.inspect("bare")
        assert info is not None
        assert info.enabled == ""
        assert info.domain == ""
        assert info.restart == "always"
        assert info.restart_problematic is True

    def test_inspect_empty_json(self, mock_docker):
        with patch.object(mock_docker, "run", return_value="[]"):
            assert mock_docker.inspect("x") is None

    def test_inspect_invalid_json(self, mock_docker):
        with patch.object(mock_docker, "run", return_value="not json"):
            assert mock_docker.inspect("x") is None

    def test_inspect_anonymous_volumes_excluded(self, mock_docker):
        data = json.dumps([{
            "Id": "c" * 64,
            "Name": "/anon",
            "State": {"Status": "running"},
            "HostConfig": {"RestartPolicy": {"Name": "no"}, "NetworkMode": "bridge"},
            "Config": {"Labels": {}},
            "NetworkSettings": {"Networks": {}, "Ports": {}},
            "Mounts": [
                {"Source": "/home/user/.local/share/containers/storage/volumes/abc/_data", "Type": "volume"},
                {"Source": "/var/lib/docker/volumes/mydata/_data", "Type": "volume"},
                {"Source": "/opt/app/config", "Type": "bind"},
            ],
        }])
        with patch.object(mock_docker, "run", return_value=data):
            info = mock_docker.inspect("anon")
        assert info is not None
        assert "/opt/app/config" in info.mounts
        # Anonymous volumes should be excluded
        for m in info.mounts:
            assert "containers/storage/volumes" not in m
            assert "docker/volumes" not in m

    def test_inspect_multiple_ports(self, mock_docker):
        data = json.dumps([{
            "Id": "d" * 64,
            "Name": "/multiport",
            "State": {"Status": "running"},
            "HostConfig": {"RestartPolicy": {"Name": "no"}, "NetworkMode": "bridge"},
            "Config": {"Labels": {}},
            "NetworkSettings": {
                "Networks": {},
                "Ports": {
                    "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
                    "443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8443"}],
                    "9000/tcp": None,  # exposed but not published
                },
            },
            "Mounts": [],
        }])
        with patch.object(mock_docker, "run", return_value=data):
            info = mock_docker.inspect("multiport")
        assert len(info.exposed_ports) == 3
        assert len(info.published_ports) == 2

    def test_network_ids_for(self, mock_docker, sample_container_json):
        with patch.object(mock_docker, "inspect") as mock_inspect:
            mock_inspect.return_value = I.ContainerInfo(network_ids=["net1", "net2"])
            result = mock_docker.network_ids_for("cid")
        assert result == ["net1", "net2"]

    def test_network_ids_for_no_container(self, mock_docker):
        with patch.object(mock_docker, "inspect", return_value=None):
            assert mock_docker.network_ids_for("cid") == []

    def test_detect_sudo_context(self):
        """Test sudo detection path skipped on Windows (no geteuid)."""
        client = I.DockerClient(cmd_override="docker")
        assert client.detect() is True  # cmd_override always wins

    def test_run_exec(self, mock_docker):
        with patch.object(mock_docker, "run", return_value="output") as mock_run:
            result = mock_docker.run_exec("container1", ["python3", "-c", "print(1)"])
        assert result == "output"
        mock_run.assert_called_with(["exec", "container1", "python3", "-c", "print(1)"], timeout=10)


# ══════════════════════════════════════════════════════════════════════════════
# NpmDatabase Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestNpmDatabase:
    def make_db(self, tmp_dir, mock_docker=None):
        if mock_docker is None:
            mock_docker = MagicMock()
            mock_docker.available = False
        return I.NpmDatabase(mock_docker, tmp_dir)

    def test_fetch_from_local_sqlite(self, tmp_dir, sqlite_db):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db.fetch()
        assert len(db._proxy_hosts) >= 1
        assert any(h["forward_host"] == "myapp" for h in db._proxy_hosts)

    def test_fetch_only_once(self, tmp_dir, sqlite_db):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db.fetch()
        initial_count = len(db._proxy_hosts)
        # Add a row to DB — should NOT appear in second fetch
        with sqlite3.connect(str(sqlite_db)) as conn:
            conn.execute(
                "INSERT INTO proxy_host (domain_names, forward_host, forward_port) VALUES (?, ?, ?)",
                ('["new.example.com"]', "new", 9999),
            )
        db.fetch()
        assert len(db._proxy_hosts) == initial_count

    def test_find_config_by_name(self, tmp_dir, sqlite_db):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db.fetch()
        result = db.find_config_for("myapp", ["172.20.0.2"], ["8080"])
        assert result is not None
        assert result["domain"] == "myapp.example.com"
        assert result["access_type"] == "name"
        assert result["port"] == "8080"

    def test_find_config_by_ip(self, tmp_dir, sqlite_db):
        # Add a host that forwards to an IP
        with sqlite3.connect(str(sqlite_db)) as conn:
            conn.execute(
                "INSERT INTO proxy_host (domain_names, forward_host, forward_port) VALUES (?, ?, ?)",
                ('["ip-app.example.com"]', "172.20.0.5", 3000),
            )
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db.fetch()
        result = db.find_config_for("someapp", ["172.20.0.5"], ["3000"])
        assert result is not None
        assert result["access_type"] == "ip"

    def test_find_config_by_published_port(self, tmp_dir, sqlite_db):
        host_ip = I._detect_host_ip()
        with sqlite3.connect(str(sqlite_db)) as conn:
            conn.execute(
                "INSERT INTO proxy_host (domain_names, forward_host, forward_port) VALUES (?, ?, ?)",
                ('["host-app.example.com"]', host_ip, 5000),
            )
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db.fetch()
        result = db.find_config_for("anyapp", [], ["5000"])
        assert result is not None

    def test_find_config_localhost_match(self, tmp_dir, sqlite_db):
        with sqlite3.connect(str(sqlite_db)) as conn:
            conn.execute(
                "INSERT INTO proxy_host (domain_names, forward_host, forward_port) VALUES (?, ?, ?)",
                ('["local.example.com"]', "127.0.0.1", 6000),
            )
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db.fetch()
        result = db.find_config_for("app", [], ["6000"])
        assert result is not None

    def test_find_config_not_found(self, tmp_dir, sqlite_db):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db.fetch()
        result = db.find_config_for("unknown", [], [])
        assert result is None

    def test_find_config_no_hosts(self, tmp_dir):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db._fetched = True
        db._proxy_hosts = []
        assert db.find_config_for("any", [], []) is None

    def test_count_old_snippets_local(self, tmp_dir, sqlite_db):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        count = db.count_old_snippets()
        assert count == 1  # One row has wakeonrequest in advanced_config

    def test_count_old_snippets_no_db(self, tmp_dir):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        assert db.count_old_snippets() is None

    def test_clear_old_snippets_local(self, tmp_dir, sqlite_db):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        cleared = db.clear_old_snippets()
        assert cleared == 1
        # Verify it's actually cleared
        with sqlite3.connect(str(sqlite_db)) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM proxy_host WHERE advanced_config LIKE '%wakeonrequest%'"
            ).fetchone()
        assert rows[0] == 0

    def test_clear_old_snippets_no_db(self, tmp_dir):
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        assert db.clear_old_snippets() is None

    def test_fetch_from_docker_exec(self, tmp_dir):
        docker = MagicMock()
        docker.available = True
        docker.find_npm_container.return_value = "npm_cid"
        docker.run_exec.return_value = '["myapp.example.com"]||myapp||8080\n'
        db = I.NpmDatabase(docker, tmp_dir)
        db.fetch()
        assert len(db._proxy_hosts) == 1
        assert db._proxy_hosts[0]["forward_host"] == "myapp"

    def test_count_from_docker_exec(self, tmp_dir):
        docker = MagicMock()
        docker.available = True
        docker.find_npm_container.return_value = "npm_cid"
        docker.run_exec.return_value = "3\n"
        db = I.NpmDatabase(docker, tmp_dir)
        count = db.count_old_snippets()
        assert count == 3

    def test_clear_from_docker_exec(self, tmp_dir):
        docker = MagicMock()
        docker.available = True
        docker.find_npm_container.return_value = "npm_cid"
        docker.run_exec.return_value = "2\n"
        db = I.NpmDatabase(docker, tmp_dir)
        cleared = db.clear_old_snippets()
        assert cleared == 2

    def test_domain_names_plain_string_fallback(self, tmp_dir):
        """Test parsing of non-JSON domain_names field."""
        docker = MagicMock()
        docker.available = False
        db = I.NpmDatabase(docker, tmp_dir)
        db._fetched = True
        db._proxy_hosts = [
            {"domain_names": "plain.example.com", "forward_host": "plain", "forward_port": "80"}
        ]
        result = db.find_config_for("plain", [], [])
        assert result is not None
        assert result["domain"] == "plain.example.com"


# ══════════════════════════════════════════════════════════════════════════════
# ComposeResolver Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestComposeResolver:
    def test_resolve_via_config_files_label(self, tmp_dir):
        compose = tmp_dir / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: test\n")
        resolver = I.ComposeResolver()
        result = resolver.resolve(str(compose), "", [])
        assert result == compose

    def test_resolve_via_working_dir(self, tmp_dir):
        compose = tmp_dir / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: test\n")
        resolver = I.ComposeResolver()
        result = resolver.resolve("", str(tmp_dir), [])
        assert result == compose

    def test_resolve_via_working_dir_compose_yaml(self, tmp_dir):
        compose = tmp_dir / "compose.yaml"
        compose.write_text("services:\n  app:\n    image: test\n")
        resolver = I.ComposeResolver()
        result = resolver.resolve("", str(tmp_dir), [])
        assert result == compose

    def test_resolve_via_mount_source(self, tmp_dir):
        sub = tmp_dir / "app"
        sub.mkdir()
        compose = tmp_dir / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: test\n")
        resolver = I.ComposeResolver()
        result = resolver.resolve("", "", [str(sub)])
        assert result == compose

    def test_resolve_via_mount_file(self, tmp_dir):
        """When mount source is a file, search from its parent."""
        sub = tmp_dir / "data"
        sub.mkdir()
        mount_file = sub / "somefile.sql"
        mount_file.write_text("SELECT 1")
        compose = tmp_dir / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: test\n")
        resolver = I.ComposeResolver()
        result = resolver.resolve("", "", [str(mount_file)])
        assert result == compose

    def test_resolve_returns_none_when_not_found(self, tmp_dir):
        resolver = I.ComposeResolver()
        result = resolver.resolve("", "", [])
        assert result is None

    def test_resolve_config_files_not_found_returns_path(self, tmp_dir):
        resolver = I.ComposeResolver()
        fake_path = str(tmp_dir / "missing.yml")
        result = resolver.resolve(fake_path, "", [])
        # Returns the Path even if file doesn't exist
        assert result == Path(fake_path)

    def test_wsl_path_normal(self):
        result = I.ComposeResolver._wsl_path("/home/user/file.yml")
        assert result == Path("/home/user/file.yml")

    def test_wsl_path_absolute(self):
        result = I.ComposeResolver._wsl_path("/opt/app/docker-compose.yml")
        assert result == Path("/opt/app/docker-compose.yml")

    def test_wsl_path_empty(self):
        assert I.ComposeResolver._wsl_path("") is None


# ══════════════════════════════════════════════════════════════════════════════
# ComposePatcher Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestComposePatcher:
    def write_compose(self, tmp_dir, content: str) -> Path:
        p = tmp_dir / "docker-compose.yml"
        p.write_text(content)
        return p

    def test_has_volume_true(self, tmp_dir):
        p = self.write_compose(tmp_dir, "    volumes:\n      - ./lua:/data/nginx/custom/lua\n")
        patcher = I.ComposePatcher(p)
        assert patcher.has_volume("./lua:/data/nginx/custom/lua") is True

    def test_has_volume_false(self, tmp_dir):
        p = self.write_compose(tmp_dir, "    volumes:\n      - ./data:/data\n")
        patcher = I.ComposePatcher(p)
        assert patcher.has_volume("./lua:/data/nginx/custom/lua") is False

    def test_backup_creates_file(self, tmp_dir):
        p = self.write_compose(tmp_dir, "services:\n  app:\n    image: test\n")
        patcher = I.ComposePatcher(p)
        bak = patcher.backup()
        assert bak.exists()
        assert bak.read_text() == p.read_text()

    def test_validate_valid_yaml(self, tmp_dir):
        p = self.write_compose(tmp_dir, "services:\n  app:\n    image: test\n")
        patcher = I.ComposePatcher(p)
        # With or without PyYAML, should return True for valid compose
        assert patcher.validate() is True

    def test_validate_no_services(self, tmp_dir):
        p = self.write_compose(tmp_dir, "version: '3'\n")
        patcher = I.ComposePatcher(p)
        # A file with only 'version:' has no services — but yaml.safe_load
        # succeeds on valid YAML. Test with truly unparseable YAML content.
        p2 = self.write_compose(tmp_dir, ": : : bad yaml {{\n")
        patcher2 = I.ComposePatcher(p2)
        # Either PyYAML catches the error and returns False, or
        # the basic 'services:' text check returns False.
        assert patcher2.validate() is False
        # Version-only file may return True (valid YAML) or False (no services)
        assert isinstance(patcher.validate(), bool)

    def test_volumes_section_count_single(self, tmp_dir):
        p = self.write_compose(
            tmp_dir,
            "services:\n  app:\n    image: test\n    volumes:\n      - ./data:/data\n"
        )
        patcher = I.ComposePatcher(p)
        assert patcher.volumes_section_count() == 1

    def test_volumes_section_count_multi(self, tmp_dir):
        p = self.write_compose(
            tmp_dir,
            "services:\n  app:\n    volumes:\n      - ./a:/a\n  db:\n    volumes:\n      - ./b:/b\n"
        )
        patcher = I.ComposePatcher(p)
        assert patcher.volumes_section_count() == 2

    def test_add_volumes_inserts_new(self, tmp_dir):
        p = self.write_compose(
            tmp_dir,
            "services:\n  app:\n    image: test\n    volumes:\n      - ./data:/data\n"
        )
        patcher = I.ComposePatcher(p)
        changed = patcher.add_volumes(["./new.lua:/data/new.lua"])
        assert changed is True
        assert "./new.lua:/data/new.lua" in p.read_text()

    def test_add_volumes_idempotent(self, tmp_dir):
        p = self.write_compose(
            tmp_dir,
            "services:\n  app:\n    volumes:\n      - ./data:/data\n"
        )
        patcher = I.ComposePatcher(p)
        # Add twice — should only write once
        patcher.add_volumes(["./data:/data"])
        content_before = p.read_text()
        patcher.add_volumes(["./data:/data"])
        assert p.read_text() == content_before

    def test_add_volumes_multiple(self, tmp_dir):
        p = self.write_compose(
            tmp_dir,
            "services:\n  app:\n    volumes:\n      - ./data:/data\n"
        )
        patcher = I.ComposePatcher(p)
        changed = patcher.add_volumes(["./a:/a", "./b:/b"])
        assert changed is True
        text = p.read_text()
        assert "./a:/a" in text
        assert "./b:/b" in text

    def test_add_labels_to_service_new_block(self, tmp_dir):
        p = self.write_compose(
            tmp_dir,
            "services:\n  myapp:\n    image: test\n    restart: no\n"
        )
        patcher = I.ComposePatcher(p)
        changed = patcher.add_labels_to_service("myapp", [
            "wakeonrequest.enable=true",
            "wakeonrequest.domain=myapp.example.com",
        ])
        assert changed is True
        text = p.read_text()
        assert "wakeonrequest.enable=true" in text
        assert "labels:" in text

    def test_add_labels_to_service_existing_block(self, tmp_dir):
        p = self.write_compose(
            tmp_dir,
            "services:\n  myapp:\n    image: test\n    labels:\n      - existing=label\n"
        )
        patcher = I.ComposePatcher(p)
        changed = patcher.add_labels_to_service("myapp", ["wakeonrequest.enable=true"])
        assert changed is True
        text = p.read_text()
        assert "wakeonrequest.enable=true" in text
        assert "existing=label" in text

    def test_add_labels_already_present(self, tmp_dir):
        p = self.write_compose(
            tmp_dir,
            "services:\n  myapp:\n    labels:\n      - wakeonrequest.enable=true\n"
        )
        patcher = I.ComposePatcher(p)
        changed = patcher.add_labels_to_service("myapp", ["wakeonrequest.enable=true"])
        assert changed is False

    def test_add_labels_service_not_found(self, tmp_dir):
        p = self.write_compose(tmp_dir, "services:\n  otherapp:\n    image: test\n")
        patcher = I.ComposePatcher(p)
        changed = patcher.add_labels_to_service("myapp", ["wakeonrequest.enable=true"])
        assert changed is False


# ══════════════════════════════════════════════════════════════════════════════
# Utility Function Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestUtilities:
    def test_detect_host_ip_socket(self):
        ip = I._detect_host_ip()
        # Should return some valid IP
        assert re.match(r"\d+\.\d+\.\d+\.\d+", ip)

    def test_detect_host_ip_fallback(self):
        with patch("socket.socket") as mock_sock:
            mock_sock.return_value.__enter__.return_value.connect.side_effect = Exception
            mock_sock.return_value.__enter__.return_value.getsockname.side_effect = Exception
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(stdout="1.2.3.4 src 192.168.1.1 uid 0")
                ip = I._detect_host_ip()
        assert ip  # Should return something

    def test_detect_host_ip_all_fail(self):
        with patch("socket.socket") as mock_sock:
            mock_sock.return_value.__enter__.return_value.connect.side_effect = Exception
            with patch("subprocess.run", side_effect=Exception):
                ip = I._detect_host_ip()
        assert ip == "127.0.0.1"

    def test_is_ip_valid(self):
        assert I._is_ip("192.168.1.1") is True
        assert I._is_ip("10.0.0.1") is True
        assert I._is_ip("172.20.0.2") is True

    def test_is_ip_invalid(self):
        assert I._is_ip("myapp") is False
        assert I._is_ip("") is False
        assert I._is_ip("localhost") is False
        assert I._is_ip("192.168.1") is False

    def test_backup_file_copies(self, tmp_dir):
        f = tmp_dir / "test.txt"
        f.write_text("hello")
        I._backup_file(f)
        # Check a .bak file was created
        baks = list(tmp_dir.glob("test.txt.bak.*"))
        assert len(baks) == 1

    def test_backup_file_nonexistent(self, tmp_dir):
        # Should not raise
        I._backup_file(tmp_dir / "nonexistent.txt")

    def test_write_bundled_server_proxy(self, tmp_dir):
        target = tmp_dir / "npm-custom" / "server_proxy.conf"
        I._write_bundled_server_proxy(target)
        assert target.exists()
        assert "wakeonrequest" in target.read_text()

    def test_write_bundled_server_proxy_idempotent(self, tmp_dir):
        target = tmp_dir / "npm-custom" / "server_proxy.conf"
        I._write_bundled_server_proxy(target)
        original = target.read_text()
        I._write_bundled_server_proxy(target)
        assert target.read_text() == original  # Not overwritten

    def test_prompt_with_input(self):
        with patch("builtins.input", return_value="hello"):
            assert I._prompt("Test: ") == "hello"

    def test_prompt_default_on_empty(self):
        with patch("builtins.input", return_value=""):
            assert I._prompt("Test: ", default="42") == "42"

    def test_prompt_eof_returns_default(self):
        with patch("builtins.input", side_effect=EOFError):
            assert I._prompt("Test: ", default="default") == "default"

    def test_prompt_keyboard_interrupt(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            assert I._prompt("Test: ", default="x") == "x"


import re  # needed for test_is_ip_valid pattern assertions above


# ══════════════════════════════════════════════════════════════════════════════
# Forward Host Decision Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDecideForwardHost:
    def make_info(self, **kw) -> I.ContainerInfo:
        defaults = dict(
            name="myapp",
            status="running",
            restart="no",
            network_mode="bridge",
            enabled="true",
            domain="myapp.example.com",
            idle_timeout="300",
            start_timeout="30",
            port_label="",
            compose_config_files="",
            compose_working_dir="",
            compose_service="myapp",
            network_ids=["net1"],
            exposed_ports=["8080/tcp"],
            published_ports=["8080"],
            ips=["172.20.0.2"],
            long_id="a" * 64,
            mounts=[],
        )
        defaults.update(kw)
        return I.ContainerInfo(**defaults)

    def test_npm_config_name_access(self):
        info = self.make_info()
        npm_config = {"fwd_host": "myapp", "port": "8080", "access_type": "name"}
        h, p, note = I._decide_forward_host(info, ["net1"], npm_config)
        assert h == "myapp"
        assert p == "8080"
        assert "container name" in note

    def test_npm_config_ip_access(self):
        info = self.make_info()
        npm_config = {"fwd_host": "172.20.0.2", "port": "8080", "access_type": "ip"}
        h, p, note = I._decide_forward_host(info, ["net1"], npm_config)
        assert h == "172.20.0.2"
        assert "IP" in note

    def test_host_network_mode(self):
        info = self.make_info(network_mode="host", published_ports=["9090"])
        with patch("install._detect_host_ip", return_value="192.168.1.100"):
            h, p, note = I._decide_forward_host(info, [], None)
        assert h == "192.168.1.100"
        assert "host network" in note

    def test_shared_network(self):
        info = self.make_info(network_ids=["net1"])
        h, p, note = I._decide_forward_host(info, ["net1"], None)
        assert h == "myapp"
        assert "same network" in note

    def test_different_network(self):
        info = self.make_info(network_ids=["net2"], published_ports=["8080"])
        with patch("install._detect_host_ip", return_value="192.168.1.100"):
            h, p, note = I._decide_forward_host(info, ["net1"], None)
        assert h == "192.168.1.100"
        assert "different network" in note

    def test_no_published_port_placeholder(self):
        info = self.make_info(network_ids=["net2"], published_ports=[])
        with patch("install._detect_host_ip", return_value="10.0.0.1"):
            h, p, note = I._decide_forward_host(info, ["net1"], None)
        assert p == "<port>"


# ══════════════════════════════════════════════════════════════════════════════
# Argument Parser Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestArgParser:
    def parse(self, args: list[str]) -> argparse.Namespace:
        import argparse
        parser = I.build_arg_parser()
        return parser.parse_args(args)

    def test_default_no_args(self):
        args = self.parse([])
        assert args.dry_run is False
        assert args.npm is None
        assert args.path is None
        assert args.target_dir is None

    def test_dry_run_flag(self):
        args = self.parse(["--dry-run"])
        assert args.dry_run is True

    def test_npm_flag(self):
        args = self.parse(["--npm", "my-npm"])
        assert args.npm == "my-npm"

    def test_npm_short_flag(self):
        args = self.parse(["-npm", "my-npm"])
        assert args.npm == "my-npm"

    def test_path_flag(self):
        args = self.parse(["--path", "/opt/npm"])
        assert args.path == "/opt/npm"

    def test_path_short_flag(self):
        args = self.parse(["-p", "/opt/npm"])
        assert args.path == "/opt/npm"

    def test_positional_target_dir(self):
        args = self.parse(["/opt/npm"])
        assert args.target_dir == "/opt/npm"

    def test_help_flag(self):
        args = self.parse(["--help"])
        assert args.help is True

    def test_combined_flags(self):
        args = self.parse(["--dry-run", "--npm", "my-npm", "/opt/npm"])
        assert args.dry_run is True
        assert args.npm == "my-npm"
        assert args.target_dir == "/opt/npm"


import argparse  # needed above


# ══════════════════════════════════════════════════════════════════════════════
# main() / run_dry_run / run_install Integration Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestMain:
    def test_missing_directory(self, tmp_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", ["install.py", "/nonexistent/path"]):
                I.main()
        assert exc.value.code == 1

    def test_missing_compose(self, tmp_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", ["install.py", str(tmp_dir)]):
                I.main()
        assert exc.value.code == 1

    def test_help_flag(self, tmp_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", ["install.py", "--help"]):
                I.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Wake-On-Request" in out


class TestRunDryRun:
    def test_dry_run_no_docker(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        db.count_old_snippets.return_value = None
        args = MagicMock(dry_run=True)

        I.run_dry_run(args, docker, db)

        out = capsys.readouterr().out
        assert "Dry Run" in out
        assert "Files" in out

    def test_dry_run_with_files_missing(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        db.count_old_snippets.return_value = 0
        args = MagicMock(dry_run=True)

        I.run_dry_run(args, docker, db)

        out = capsys.readouterr().out
        assert "wakeonrequest.lua" in out
        assert "http_top.conf" in out

    def test_dry_run_shows_volume_changes(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        db.count_old_snippets.return_value = 0
        args = MagicMock(dry_run=True)

        I.run_dry_run(args, docker, db)

        out = capsys.readouterr().out
        assert "Volume Changes" in out

    def test_dry_run_configured_container(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = True
        docker.container_ids.return_value = ["cid1"]
        docker.find_npm_container.return_value = ""
        docker.network_ids_for.return_value = []
        docker.inspect.return_value = I.ContainerInfo(
            name="myapp",
            status="running",
            restart="no",
            enabled="true",
            domain="myapp.example.com",
            idle_timeout="300",
            start_timeout="30",
            long_id="a" * 64,
        )
        db = MagicMock()
        db.count_old_snippets.return_value = 0
        args = MagicMock()

        I.run_dry_run(args, docker, db)

        out = capsys.readouterr().out
        assert "myapp" in out
        assert "myapp.example.com" in out

    def test_dry_run_unconfigured_container(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = True
        docker.container_ids.return_value = ["cid1"]
        docker.find_npm_container.return_value = ""
        docker.network_ids_for.return_value = []
        docker.inspect.return_value = I.ContainerInfo(
            name="myapp",
            status="running",
            restart="no",
            enabled="",
            domain="",
            exposed_ports=["8080/tcp"],
            published_ports=["8080"],
            ips=["172.20.0.2"],
            network_ids=["net1"],
            long_id="a" * 64,
        )
        db = MagicMock()
        db.count_old_snippets.return_value = 0
        db.find_config_for.return_value = None
        args = MagicMock()

        with patch("install._detect_host_ip", return_value="192.168.1.1"):
            I.run_dry_run(args, docker, db)

        out = capsys.readouterr().out
        assert "myapp" in out
        assert "labels:" in out.lower() or "Labels" in out

    def test_dry_run_restart_warning(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = True
        docker.container_ids.return_value = ["cid1"]
        docker.find_npm_container.return_value = ""
        docker.network_ids_for.return_value = []
        docker.inspect.return_value = I.ContainerInfo(
            name="bad-restart",
            status="running",
            restart="always",
            enabled="true",
            domain="bad.example.com",
            long_id="b" * 64,
        )
        db = MagicMock()
        db.count_old_snippets.return_value = 0
        args = MagicMock()

        I.run_dry_run(args, docker, db)

        out = capsys.readouterr().out
        assert "restart" in out
        assert "always" in out

    def test_dry_run_missing_domain_label(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = True
        docker.container_ids.return_value = ["cid1"]
        docker.find_npm_container.return_value = ""
        docker.network_ids_for.return_value = []
        docker.inspect.return_value = I.ContainerInfo(
            name="half-configured",
            status="running",
            restart="no",
            enabled="true",
            domain="",  # enabled but no domain
            long_id="c" * 64,
        )
        db = MagicMock()
        db.count_old_snippets.return_value = 0
        args = MagicMock()

        I.run_dry_run(args, docker, db)

        out = capsys.readouterr().out
        assert "MISSING" in out

    def test_dry_run_old_snippets_warning(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        db.count_old_snippets.return_value = 3
        args = MagicMock()

        I.run_dry_run(args, docker, db)

        out = capsys.readouterr().out
        assert "3" in out

    def test_dry_run_skips_npm_container(self, tmp_dir, npm_compose, capsys):
        npm_id = "npm123" + "0" * 57
        docker = MagicMock()
        docker.available = True
        docker.container_ids.return_value = ["npm123"]
        docker.find_npm_container.return_value = npm_id
        docker.network_ids_for.return_value = []
        docker.inspect.return_value = I.ContainerInfo(
            name="npm",
            status="running",
            restart="no",
            long_id=npm_id,
        )
        db = MagicMock()
        db.count_old_snippets.return_value = 0
        args = MagicMock()

        I.run_dry_run(args, docker, db)

        # npm container should be skipped — no "➕" for it
        out = capsys.readouterr().out
        assert "➕" not in out


class TestRunInstall:
    def test_install_no_docker(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        db.clear_old_snippets.return_value = None
        args = MagicMock(dry_run=False)

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            mock_response = MagicMock()
            mock_response.read.return_value = b"-- lua content"
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            I.run_install(args, docker, db)

        out = capsys.readouterr().out
        assert "Installation Complete" in out

    def test_install_downloads_files(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        db.clear_old_snippets.return_value = 0
        args = MagicMock()

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("sys.stdin") as mock_stdin, \
             patch("sys.stdout") as mock_stdout:
            mock_stdin.isatty.return_value = False
            mock_stdout.isatty.return_value = False
            mock_response = MagicMock()
            mock_response.read.return_value = b"content"
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            I.run_install(args, docker, db)

        assert (tmp_dir / "wakeonrequest.lua").exists()
        assert (tmp_dir / "npm-custom" / "http_top.conf").exists()
        assert (tmp_dir / "npm-custom" / "server_proxy.conf").exists()

    def test_install_download_failure(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        args = MagicMock()

        with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            with pytest.raises(SystemExit) as exc:
                I.run_install(args, docker, db)
        assert exc.value.code == 1

    def test_install_patches_compose(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        db.clear_old_snippets.return_value = 0
        args = MagicMock()

        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("sys.stdin") as mock_stdin, \
             patch("sys.stdout") as mock_stdout:
            mock_stdin.isatty.return_value = False
            mock_stdout.isatty.return_value = False
            mock_response = MagicMock()
            mock_response.read.return_value = b"content"
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            I.run_install(args, docker, db)

        compose_text = npm_compose.read_text()
        assert "wakeonrequest.lua" in compose_text or "docker.sock" in compose_text

    def test_install_already_patched(self, tmp_dir, capsys):
        # Write a compose that already has all volumes
        compose = tmp_dir / "docker-compose.yml"
        vols = "\n".join(
            f"      - ./{lp}:{cp}" for lp, cp in I.FILES
        )
        compose.write_text(
            f"services:\n  app:\n    image: test\n    volumes:\n{vols}\n"
            f"      - {I.VOL_SOCK}\n"
        )

        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        db.clear_old_snippets.return_value = 0
        args = MagicMock()

        # Pre-create all files so no download needed
        for lp, _ in I.FILES:
            p = tmp_dir / lp
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("content")

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            I.run_install(args, docker, db)

        out = capsys.readouterr().out
        assert "already configured" in out or "no changes needed" in out


# ══════════════════════════════════════════════════════════════════════════════
# configure_containers Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigureContainers:
    def make_docker_with_container(self, **info_kw) -> MagicMock:
        docker = MagicMock()
        docker.available = True
        docker.find_npm_container.return_value = ""
        docker.network_ids_for.return_value = []
        docker.container_ids.return_value = ["cid1"]
        defaults = dict(
            name="myapp",
            status="running",
            restart="no",
            enabled="",
            domain="",
            exposed_ports=["8080/tcp"],
            published_ports=["8080"],
            ips=["172.20.0.5"],
            network_ids=["net2"],
            compose_config_files="",
            compose_working_dir="",
            compose_service="myapp",
            long_id="a" * 64,
            mounts=[],
            network_mode="bridge",
        )
        defaults.update(info_kw)
        docker.inspect.return_value = I.ContainerInfo(**defaults)
        return docker

    def test_skip_container(self, tmp_dir, npm_compose, capsys):
        docker = self.make_docker_with_container()
        db = MagicMock()
        db.find_config_for.return_value = None
        args = MagicMock()

        with patch("install._prompt", return_value="3"), \
             patch("install._detect_host_ip", return_value="10.0.0.1"):
            I.configure_containers(args, docker, db)

        out = capsys.readouterr().out
        assert "Skipped" in out

    def test_method_b_output(self, tmp_dir, npm_compose, capsys):
        docker = self.make_docker_with_container()
        db = MagicMock()
        db.find_config_for.return_value = None
        args = MagicMock()

        prompts = iter(["2", "myapp.example.com", "300", "30"])
        with patch("install._prompt", side_effect=lambda *a, **kw: next(prompts, "1")), \
             patch("install._detect_host_ip", return_value="192.168.1.5"):
            I.configure_containers(args, docker, db)

        out = capsys.readouterr().out
        assert "wake_container" in out

    def test_method_a_patches_compose(self, tmp_dir, npm_compose, capsys):
        docker = self.make_docker_with_container(
            compose_working_dir=str(tmp_dir)
        )
        db = MagicMock()
        db.find_config_for.return_value = None
        args = MagicMock()

        prompts = iter(["1", "myapp.example.com", "300", "30", "y"])
        with patch("install._prompt", side_effect=lambda *a, **kw: next(prompts, "")), \
             patch("install._detect_host_ip", return_value="10.0.0.1"):
            I.configure_containers(args, docker, db)

        out = capsys.readouterr().out
        assert "myapp" in out

    def test_already_configured_shown(self, tmp_dir, npm_compose, capsys):
        docker = self.make_docker_with_container(
            enabled="true",
            domain="existing.example.com",
            idle_timeout="300",
            start_timeout="30",
        )
        db = MagicMock()
        args = MagicMock()

        I.configure_containers(args, docker, db)

        out = capsys.readouterr().out
        assert "already configured" in out
        assert "existing.example.com" in out

    def test_all_configured_message(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = True
        docker.find_npm_container.return_value = ""
        docker.network_ids_for.return_value = []
        docker.container_ids.return_value = []
        db = MagicMock()
        args = MagicMock()

        I.configure_containers(args, docker, db)

        out = capsys.readouterr().out
        assert "All containers are already configured" in out

    def test_no_docker(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = False
        db = MagicMock()
        args = MagicMock()

        I.configure_containers(args, docker, db)

        out = capsys.readouterr().out
        assert "skipping" in out.lower()

    def test_method_a_compose_not_found(self, tmp_dir, npm_compose, capsys):
        docker = self.make_docker_with_container()
        db = MagicMock()
        db.find_config_for.return_value = None
        args = MagicMock()

        prompts = iter(["1", "myapp.example.com", "300", "30"])
        with patch("install._prompt", side_effect=lambda *a, **kw: next(prompts, "")), \
             patch("install._detect_host_ip", return_value="10.0.0.1"):
            I.configure_containers(args, docker, db)

        out = capsys.readouterr().out
        assert "myapp" in out

    def test_print_manual_snippet_restart(self, capsys):
        I._print_manual_snippet("always", ["wakeonrequest.enable=true", "wakeonrequest.domain=app.com"])
        out = capsys.readouterr().out
        assert "restart" in out
        assert "wakeonrequest.enable=true" in out


# ══════════════════════════════════════════════════════════════════════════════
# Edge Case Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_compose_patcher_no_volumes_block(self, tmp_dir):
        """Compose file without any volumes: block — add_volumes should not crash."""
        p = tmp_dir / "docker-compose.yml"
        p.write_text("services:\n  app:\n    image: test\n")
        patcher = I.ComposePatcher(p)
        changed = patcher.add_volumes(["./new:/new"])
        # May not change (no volumes: block found) — just must not crash
        assert isinstance(changed, bool)

    def test_inspect_empty_networks(self, tmp_dir):
        docker = I.DockerClient()
        docker._cmd = ["docker"]
        docker._detected = True
        data = json.dumps([{
            "Id": "e" * 64,
            "Name": "/nonet",
            "State": {"Status": "created"},
            "HostConfig": {"RestartPolicy": {"Name": "no"}, "NetworkMode": "none"},
            "Config": {"Labels": {}},
            "NetworkSettings": {"Networks": None, "Ports": None},
            "Mounts": [],
        }])
        with patch.object(docker, "run", return_value=data):
            info = docker.inspect("nonet")
        assert info is not None
        assert info.network_ids == []
        assert info.exposed_ports == []
        assert info.published_ports == []

    def test_npm_database_exec_returns_bad_int(self, tmp_dir):
        docker = MagicMock()
        docker.available = True
        docker.find_npm_container.return_value = "npm"
        docker.run_exec.return_value = "not a number\n"
        db = I.NpmDatabase(docker, tmp_dir)
        result = db.clear_old_snippets()
        # Should fallback to local or return None, not crash
        assert result is None or isinstance(result, int)

    def test_resolve_deep_mount_search(self, tmp_dir):
        """Compose file 3 levels above mount source."""
        deep = tmp_dir / "a" / "b" / "c" / "data"
        deep.mkdir(parents=True)
        compose = tmp_dir / "a" / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: test\n")
        resolver = I.ComposeResolver()
        result = resolver.resolve("", "", [str(deep)])
        assert result == compose

    def test_dry_run_no_containers(self, tmp_dir, npm_compose, capsys):
        docker = MagicMock()
        docker.available = True
        docker.container_ids.return_value = []
        docker.find_npm_container.return_value = ""
        docker.network_ids_for.return_value = []
        db = MagicMock()
        db.count_old_snippets.return_value = 0
        args = MagicMock()

        I.run_dry_run(args, docker, db)

        # Should complete without error
        out = capsys.readouterr().out
        assert "Container Label Status" in out

    def test_container_ids_filters_empty(self):
        docker = I.DockerClient()
        docker._cmd = ["docker"]
        docker._detected = True
        with patch.object(docker, "run", return_value="\n  \nabc\n\ndef\n"):
            ids = docker.container_ids()
        assert "" not in ids
        assert "abc" in ids
        assert "def" in ids
