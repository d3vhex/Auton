"""Tests for the queue authentication (Phase 5).

The task queue is the one component every pod talks to, so it is the one
component that must never accept an unauthenticated client. These tests pin
both halves of that contract:

* the **code** — a missing password, a wrong password and a password against
  a passwordless server all raise `QueueError`, so the caller refuses to
  start. There is no "connect without auth to be nice" fallback;
* the **manifests** — the server requires a password, every client reads it
  from the mounted Secret, and no manifest ever contains a literal password.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

import ops_queue  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
YAMLS = ROOT / "yamls"
CHART = ROOT / "charts" / "auton-chart"


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #

class AuthenticationError(Exception):
    """Mirrors redis.exceptions.AuthenticationError."""


class ResponseError(Exception):
    """Mirrors redis.exceptions.ResponseError."""


class AuthRedis:
    """A Redis double that answers the way a server answers an auth mismatch.

    ``mode``:
      ok                    -> PING works
      noauth                -> the server requires a password, none was sent
      wrongpass             -> the password does not match
      auth-without-password -> the server has no password, AUTH was sent
    """

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.pings = 0

    def ping(self) -> bool:
        self.pings += 1
        if self.mode == "ok":
            return True
        if self.mode == "noauth":
            raise AuthenticationError("Authentication required.")
        if self.mode == "wrongpass":
            raise ResponseError("WRONGPASS invalid username-password pair "
                                "or user is disabled.")
        if self.mode == "auth-without-password":
            raise ResponseError("ERR Client sent AUTH, but no password is set")
        raise AssertionError(f"unknown mode {self.mode}")


# --------------------------------------------------------------------------- #
# The password source
# --------------------------------------------------------------------------- #

class TestPasswordSource:
    def test_the_environment_variable_is_used(self, monkeypatch):
        monkeypatch.delenv("AGENT_REDIS_PASSWORD_FILE", raising=False)
        monkeypatch.setenv("AGENT_REDIS_PASSWORD", "from-env")
        assert ops_queue.redis_password() == "from-env"

    def test_the_file_wins_over_the_environment(self, monkeypatch, tmp_path):
        key = tmp_path / "password"
        key.write_text("from-file\n")
        monkeypatch.setenv("AGENT_REDIS_PASSWORD", "from-env")
        monkeypatch.setenv("AGENT_REDIS_PASSWORD_FILE", str(key))
        assert ops_queue.redis_password() == "from-file"

    def test_no_password_at_all_is_empty(self, monkeypatch):
        monkeypatch.delenv("AGENT_REDIS_PASSWORD_FILE", raising=False)
        monkeypatch.delenv("AGENT_REDIS_PASSWORD", raising=False)
        assert ops_queue.redis_password() == ""

    def test_an_unreadable_file_fails_closed(self, monkeypatch, tmp_path):
        missing = tmp_path / "nope" / "password"
        monkeypatch.setenv("AGENT_REDIS_PASSWORD_FILE", str(missing))
        with pytest.raises(ops_queue.QueueError, match="cannot read"):
            ops_queue.redis_password()

    def test_an_empty_file_fails_closed(self, monkeypatch, tmp_path):
        # An empty file must NOT degrade into "no password": that would make
        # the client look healthy against a passwordless server.
        key = tmp_path / "password"
        key.write_text("   \n")
        monkeypatch.setenv("AGENT_REDIS_PASSWORD_FILE", str(key))
        with pytest.raises(ops_queue.QueueError, match="is empty"):
            ops_queue.redis_password()


# --------------------------------------------------------------------------- #
# The client fails closed
# --------------------------------------------------------------------------- #

class TestClientFailsClosed:
    @pytest.mark.parametrize("mode", ["noauth", "wrongpass",
                                      "auth-without-password"])
    def test_every_auth_mismatch_is_a_queue_error(self, mode):
        backend = ops_queue.RedisBackend(password="whatever",
                                         client=AuthRedis(mode))
        with pytest.raises(ops_queue.QueueError):
            backend.ping()

    def test_an_auth_error_is_not_reported_as_an_outage(self):
        # "unreachable" would send an operator looking at the network; the
        # real problem is the password.
        backend = ops_queue.RedisBackend(password="whatever",
                                         client=AuthRedis("wrongpass"))
        with pytest.raises(ops_queue.QueueError, match="authentication failed"):
            backend.ping()

    def test_a_connection_failure_is_still_an_outage(self):
        backend = ops_queue.RedisBackend(password="whatever",
                                         client=AuthRedis("noauth"))
        backend._client.mode = "noauth"

        class Dead:
            def ping(self):
                raise OSError("connection refused")

        backend._client = Dead()
        with pytest.raises(ops_queue.QueueError, match="unreachable"):
            backend.ping()

    def test_the_backend_reports_how_it_authenticates(self):
        assert ops_queue.RedisBackend(password="x",
                                      client=AuthRedis()).auth == "password"
        assert ops_queue.RedisBackend(client=AuthRedis()).auth == "none"

    def test_create_backend_refuses_an_unauthenticated_client(self, monkeypatch):
        monkeypatch.setenv("AGENT_QUEUE", "redis")
        monkeypatch.delenv("AGENT_REDIS_PASSWORD_FILE", raising=False)
        monkeypatch.delenv("AGENT_REDIS_PASSWORD", raising=False)
        with pytest.raises(ops_queue.QueueError):
            ops_queue.create_backend(client=AuthRedis("noauth"))

    def test_create_backend_refuses_a_wrong_password(self, monkeypatch):
        monkeypatch.setenv("AGENT_QUEUE", "redis")
        monkeypatch.delenv("AGENT_REDIS_PASSWORD_FILE", raising=False)
        monkeypatch.setenv("AGENT_REDIS_PASSWORD", "wrong")
        with pytest.raises(ops_queue.QueueError):
            ops_queue.create_backend(client=AuthRedis("wrongpass"))

    def test_create_backend_refuses_a_password_on_a_passwordless_server(
            self, monkeypatch):
        monkeypatch.setenv("AGENT_QUEUE", "redis")
        monkeypatch.delenv("AGENT_REDIS_PASSWORD_FILE", raising=False)
        monkeypatch.setenv("AGENT_REDIS_PASSWORD", "some-password")
        with pytest.raises(ops_queue.QueueError):
            ops_queue.create_backend(client=AuthRedis("auth-without-password"))

    def test_a_good_password_connects(self, monkeypatch):
        monkeypatch.setenv("AGENT_QUEUE", "redis")
        monkeypatch.delenv("AGENT_REDIS_PASSWORD_FILE", raising=False)
        monkeypatch.setenv("AGENT_REDIS_PASSWORD", "the-right-password")
        backend = ops_queue.create_backend(client=AuthRedis("ok"))
        assert backend.auth == "password"


class TestAuthClassification:
    @pytest.mark.parametrize("text", [
        "Authentication required.",
        "NOAUTH Authentication required.",
        "WRONGPASS invalid username-password pair or user is disabled.",
        "ERR Client sent AUTH, but no password is set",
        "invalid password",
    ])
    def test_auth_failures_are_recognised(self, text):
        assert ops_queue._is_auth_error(ResponseError(text))

    def test_other_failures_are_not_auth_failures(self):
        assert not ops_queue._is_auth_error(ResponseError("connection reset"))
        assert not ops_queue._is_auth_error(OSError("timed out"))


# --------------------------------------------------------------------------- #
# The manifests
# --------------------------------------------------------------------------- #

def _docs(name: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all((YAMLS / name).read_text())
            if doc]


CLIENTS = ("agent-deployment.yaml", "auton-ops-deployment.yaml",
           "auton-scanner-deployment.yaml", "auton-worker-deployment.yaml")


def _container(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]["containers"][0]


class TestRedisServerManifest:
    def _deployment(self) -> dict:
        return next(d for d in _docs("redis.yaml") if d["kind"] == "Deployment")

    def test_the_server_requires_a_password(self):
        init = self._deployment()["spec"]["template"]["spec"]["initContainers"][0]
        script = init["command"][2]
        assert "requirepass $pw" in script
        # The password is read from the mounted Secret, never from a literal.
        assert 'cat /etc/auton-redis/password' in script

    def test_the_password_is_never_a_command_line_argument(self):
        container = _container(self._deployment())
        assert container["command"] == ["redis-server", "/tmp/redis/redis.conf"]
        joined = " ".join(container["command"])
        assert "requirepass" not in joined

    def test_an_empty_or_unsafe_password_stops_the_server(self):
        init = self._deployment()["spec"]["template"]["spec"]["initContainers"][0]
        script = init["command"][2]
        assert "is empty" in script
        assert "at least 16 characters" in script
        assert "[!A-Za-z0-9_-]" in script

    def test_the_probes_authenticate(self):
        container = _container(self._deployment())
        for probe in (container["readinessProbe"], container["livenessProbe"]):
            command = probe["exec"]["command"][-1]
            assert "REDISCLI_AUTH" in command
            assert "PONG" in command

    def test_the_server_mounts_the_secret(self):
        volumes = self._deployment()["spec"]["template"]["spec"]["volumes"]
        auth = next(v for v in volumes if v["name"] == "redis-auth")
        assert auth["secret"]["secretName"] == "auton-redis"
        assert auth["secret"]["items"] == [{"key": "password",
                                            "path": "password"}]


class TestClientManifests:
    @pytest.mark.parametrize("name", CLIENTS)
    def test_the_client_reads_the_password_from_the_secret(self, name):
        deployment = next(d for d in _docs(name) if d["kind"] == "Deployment")
        container = _container(deployment)
        env = {item["name"]: item.get("value")
               for item in container["env"] if "value" in item}
        assert env["AGENT_REDIS_PASSWORD_FILE"] == "/etc/auton-redis/password"
        # The old env var must not come back: a value in the environment is
        # visible in every process dump.
        assert "AGENT_REDIS_PASSWORD" not in env

    @pytest.mark.parametrize("name", CLIENTS)
    def test_the_client_mounts_the_secret(self, name):
        deployment = next(d for d in _docs(name) if d["kind"] == "Deployment")
        spec = deployment["spec"]["template"]["spec"]
        mounts = {m["name"]: m for m in _container(deployment)["volumeMounts"]}
        assert mounts["redis-auth"]["mountPath"] == "/etc/auton-redis"
        assert mounts["redis-auth"]["readOnly"] is True
        volumes = {v["name"]: v for v in spec["volumes"]}
        assert volumes["redis-auth"]["secret"]["secretName"] == "auton-redis"

    @pytest.mark.parametrize("name", CLIENTS)
    def test_the_client_uses_the_redis_queue(self, name):
        deployment = next(d for d in _docs(name) if d["kind"] == "Deployment")
        env = {item["name"]: item.get("value")
               for item in _container(deployment)["env"] if "value" in item}
        assert env["AGENT_QUEUE"] == "redis"


class TestNoLiteralPassword:
    """No manifest may ever contain a password value."""

    @pytest.mark.parametrize("path", sorted(YAMLS.glob("*.yaml")),
                             ids=lambda p: p.name)
    def test_no_manifest_carries_a_password_value(self, path):
        text = path.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # An env var with an inline value would put the password in the
            # pod spec (and in every `kubectl get pod -o yaml`).
            if stripped == "- name: AGENT_REDIS_PASSWORD":
                raise AssertionError(
                    f"{path.name}: the password must not be an env var")
            if "requirepass " in stripped and "$" not in stripped:
                raise AssertionError(
                    f"{path.name}: requirepass must read the Secret")

    def test_the_chart_does_not_carry_a_password_value(self):
        for path in sorted(CHART.glob("templates/*.yaml")):
            text = path.read_text()
            assert "- name: AGENT_REDIS_PASSWORD\n" not in text, path.name
            # The only `requirepass` is the shell variable in the init
            # container; a literal would be a password in the chart.
            for line in text.splitlines():
                if "requirepass " in line:
                    assert "requirepass $pw" in line, path.name

    def test_the_chart_creates_the_secret(self):
        text = (CHART / "templates" / "secrets.yaml").read_text()
        assert "auton.redisPassword" in text
        assert "password:" in text
        # The generated value must not be a plain value in the template.
        assert "password: {{ .Values.queue.redisPassword }}" not in text


# --------------------------------------------------------------------------- #
# The chart
# --------------------------------------------------------------------------- #

class TestChart:
    def _template(self, name: str) -> str:
        return (CHART / "templates" / name).read_text()

    def test_the_chart_server_requires_a_password(self):
        text = self._template("redis.yaml")
        assert "requirepass $pw" in text
        assert 'redis-server", "/tmp/redis/redis.conf' in text

    def test_every_chart_client_reads_the_password_file(self):
        for name in ("deployment-agent.yaml", "deployment-ops.yaml",
                     "deployment-scanner.yaml", "deployment-worker.yaml"):
            text = self._template(name)
            assert "AGENT_REDIS_PASSWORD_FILE" in text, name
            assert "redis-auth" in text, name

    def test_the_chart_validates_the_password(self):
        text = self._template("validate.yaml")
        assert "queue.redisAuth" in text
        assert "A-Za-z0-9_-" in text
