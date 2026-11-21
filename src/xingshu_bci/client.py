"""XingShuClient: high-level wrapper over the local REST + WebSocket API."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote, urlparse

import httpx
import websocket  # from websocket-client

from .discovery import load_bootstrap
from .events import Event
from .exceptions import (
    XingShuAuthError,
    XingShuConnectionError,
    XingShuError,
    XingShuForbiddenError,
    XingShuNotFoundError,
    XingShuRateLimitError,
    XingShuTimeoutError,
)
from .models import (
    ExperimentModelRegistration,
    ServerStatus,
    SsvepPrediction,
    SsvepTrialWindow,
    TokenCreated,
    TokenInfo,
    VersionInfo,
)

logger = logging.getLogger("xingshu_bci")


def healthz(base_url: Optional[str] = None, *, timeout: float = 3.0, trust_env: bool = False) -> bool:
    """Check local API liveness without a token.

    ``/healthz`` is unauthenticated, so this works before a token exists.
    When ``base_url`` is omitted the bootstrap file written by the desktop
    app is consulted; a missing bootstrap simply returns ``False``.
    """
    if base_url is None:
        bootstrap = load_bootstrap() or {}
        base_url = str(bootstrap.get("baseUrl") or "")
    if not base_url:
        return False
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/healthz",
            timeout=timeout,
            trust_env=trust_env,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


class _CombinedStopEvent:
    def __init__(self, *events: Any):
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class XingShuClient:
    """High-level synchronous client for the XingShu BCI local API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        ws_url: Optional[str] = None,
        timeout: float = 30.0,
        verify: bool = True,
        trust_env: bool = False,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.ws_url = ws_url or self._derive_ws_url(self.base_url)
        self.timeout = timeout
        self.trust_env = bool(trust_env)
        self._http = httpx.Client(
            # httpx treats a leading slash as an absolute path and would
            # discard a reverse-proxy prefix. Keep a trailing slash here and
            # strip leading slashes in request() so /v1/... stays under it.
            base_url=f"{self.base_url}/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=verify,
            trust_env=self.trust_env,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "XingShuClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── Auto discovery ─────────────────────────────────────────────────

    @classmethod
    def auto(cls, *, token: Optional[str] = None, **kwargs: Any) -> "XingShuClient":
        """Discover the local API via bootstrap.json.

        ``token`` may be provided explicitly, via the ``XINGSHU_BCI_TOKEN``
        environment variable, or omitted (in which case the user is expected
        to create one in the Developer Center first).
        """
        bootstrap = load_bootstrap()
        if not bootstrap or not bootstrap.get("baseUrl"):
            raise XingShuConnectionError(
                "未找到 XingShu BCI 本地 API。请确认桌面应用已运行并启用了 API 服务。",
                code="BOOTSTRAP_MISSING",
            )
        if token is None:
            token = os.environ.get("XINGSHU_BCI_TOKEN", "")
        if not token:
            raise XingShuConnectionError(
                "缺少 API Token。请在「开发者中心」中创建并通过 token= 参数或 XINGSHU_BCI_TOKEN 环境变量传入。",
                code="TOKEN_MISSING",
            )
        return cls(
            base_url=bootstrap["baseUrl"],
            token=token,
            ws_url=bootstrap.get("wsUrl"),
            **kwargs,
        )

    # ── HTTP helpers ───────────────────────────────────────────────────

    @staticmethod
    def _derive_ws_url(base_url: str) -> str:
        parsed = urlparse(base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        prefix = parsed.path.rstrip("/")
        return f"{scheme}://{parsed.netloc}{prefix}/v1/events"

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        relative_path = path.lstrip("/") if isinstance(path, str) else path
        try:
            response = self._http.request(method.upper(), relative_path, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise XingShuTimeoutError(str(exc), code="TIMEOUT") from exc
        except httpx.RequestError as exc:
            raise XingShuConnectionError(str(exc), code="CONNECTION_ERROR") from exc
        if response.status_code >= 400:
            self._raise_for_response(response)
        return response

    def _raise_for_response(self, response: httpx.Response) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        code = (error or {}).get("code", "ERROR")
        message = (error or {}).get("message") or response.text or "API request failed"
        request_id = payload.get("requestId") if isinstance(payload, dict) else None

        cls = XingShuError
        if response.status_code == 401:
            cls = XingShuAuthError
        elif response.status_code == 403:
            cls = XingShuForbiddenError
        elif response.status_code == 404:
            cls = XingShuNotFoundError
        elif response.status_code == 408:
            cls = XingShuTimeoutError
        elif response.status_code == 429:
            cls = XingShuRateLimitError

        raise cls(
            message,
            code=code,
            status=response.status_code,
            details=(error or {}).get("details"),
            request_id=request_id,
        )

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.request(method, path, **kwargs).json()

    # ── System ─────────────────────────────────────────────────────────

    def version(self) -> VersionInfo:
        return VersionInfo.from_dict(self._json("GET", "/v1/version"))

    def status(self) -> ServerStatus:
        return ServerStatus.from_dict(self._json("GET", "/v1/status"))

    def healthz(self) -> bool:
        try:
            return self._http.get("healthz").status_code == 200
        except httpx.RequestError:
            return False

    def list_serial_ports(self) -> List[Dict[str, Any]]:
        return self._json("GET", "/v1/ports/serial").get("ports", [])

    # ── Device ─────────────────────────────────────────────────────────

    def connect(self, **payload: Any) -> Dict[str, Any]:
        return self._json("POST", "/v1/device/connect", json=payload)

    def pause(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/device/pause")

    def resume(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/device/resume")

    def disconnect(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/device/disconnect")

    # ── Config ─────────────────────────────────────────────────────────

    def get_filter_config(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/config/filter")

    def set_filter_config(self, **payload: Any) -> Dict[str, Any]:
        return self._json("POST", "/v1/config/filter", json=payload)

    def get_analysis_config(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/config/analysis")

    def set_analysis_config(self, **payload: Any) -> Dict[str, Any]:
        return self._json("POST", "/v1/config/analysis", json=payload)

    # ── Recording ──────────────────────────────────────────────────────

    def recording_status(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/recording/status")

    def start_recording(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/recording/start")

    def pause_recording(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/recording/pause")

    def resume_recording(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/recording/resume")

    def stop_recording(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/recording/stop")

    def insert_marker(self, *, value: float, label: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"value": value}
        if label is not None:
            body["label"] = label
        return self._json("POST", "/v1/recording/markers", json=body)

    # ── Playback ───────────────────────────────────────────────────────

    def open_playback(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/playback/open")

    def start_playback(self, *, speed: Optional[float] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if speed is not None:
            body["speed"] = speed
        return self._json("POST", "/v1/playback/start", json=body)

    def pause_playback(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/playback/pause")

    def seek_playback(self, position_samples: int) -> Dict[str, Any]:
        return self._json("POST", "/v1/playback/seek", json={"positionSamples": position_samples})

    def stop_playback(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/playback/stop")

    def export_playback_bdf(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/playback/export/bdf")

    # ── Signal Quality ─────────────────────────────────────────────────

    def signal_quality_latest(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/signal-quality/latest")

    def start_signal_check(self, **payload: Any) -> Dict[str, Any]:
        return self._json("POST", "/v1/signal-quality/start", json=payload)

    def stop_signal_check(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/signal-quality/stop")

    # ── Networking ─────────────────────────────────────────────────────

    def get_networking_config(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/networking/config")

    def set_networking_config(self, **payload: Any) -> Dict[str, Any]:
        return self._json("POST", "/v1/networking/config", json=payload)

    def networking_status(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/networking/status")

    def start_networking(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/networking/start")

    def stop_networking(self) -> Dict[str, Any]:
        return self._json("POST", "/v1/networking/stop")

    # ── Analysis ───────────────────────────────────────────────────────

    def analysis_latest(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/analysis/latest")

    def analysis_focus_latest(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/analysis/focus/latest")

    def analysis_bands_latest(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/analysis/bands/latest")

    # ── Logs ───────────────────────────────────────────────────────────

    def logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self._json("GET", "/v1/logs", params={"limit": limit}).get("items", [])

    # ── Tokens ─────────────────────────────────────────────────────────

    def list_tokens(self) -> List[TokenInfo]:
        data = self._json("GET", "/v1/auth/tokens").get("tokens", [])
        return [TokenInfo.from_dict(item) for item in data]

    def create_token(
        self,
        *,
        name: Optional[str] = None,
        scopes: Optional[Sequence[str]] = None,
        expires_in_days: Optional[int] = None,
    ) -> TokenCreated:
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if scopes is not None:
            body["scopes"] = list(scopes)
        if expires_in_days is not None:
            body["expiresInDays"] = expires_in_days
        return TokenCreated.from_dict(self._json("POST", "/v1/auth/tokens", json=body))

    def revoke_token(self, token_id: str) -> Dict[str, Any]:
        return self._json("POST", "/v1/auth/tokens/revoke", json={"id": token_id})

    # ── External experiment models ────────────────────────────────────

    def list_experiment_models(self) -> List[ExperimentModelRegistration]:
        data = self._json("GET", "/v1/experiments/models").get("models", [])
        return [ExperimentModelRegistration.from_dict(item) for item in data]

    def register_experiment_model(
        self,
        name: str,
        *,
        version: Optional[str] = None,
        paradigm: str = "ssvep",
    ) -> ExperimentModelRegistration:
        if not str(name).strip():
            raise ValueError("name is required")
        body: Dict[str, Any] = {"name": str(name).strip(), "paradigm": paradigm}
        if version is not None:
            body["version"] = str(version)
        return ExperimentModelRegistration.from_dict(
            self._json("POST", "/v1/experiments/models/register", json=body)
        )

    def heartbeat_experiment_model(self, model_id: str) -> Dict[str, Any]:
        if not str(model_id).strip():
            raise ValueError("model_id is required")
        encoded_model_id = quote(str(model_id).strip(), safe="")
        return self._json(
            "POST",
            f"/v1/experiments/models/{encoded_model_id}/heartbeat",
        )

    def unregister_experiment_model(self, model_id: str) -> Dict[str, Any]:
        if not str(model_id).strip():
            raise ValueError("model_id is required")
        encoded_model_id = quote(str(model_id).strip(), safe="")
        return self._json("DELETE", f"/v1/experiments/models/{encoded_model_id}")

    def register_ssvep_model(
        self,
        name: str,
        *,
        version: Optional[str] = None,
    ) -> ExperimentModelRegistration:
        return self.register_experiment_model(name, version=version, paradigm="ssvep")

    def heartbeat_ssvep_model(self, model_id: str) -> Dict[str, Any]:
        return self.heartbeat_experiment_model(model_id)

    def unregister_ssvep_model(self, model_id: str) -> Dict[str, Any]:
        return self.unregister_experiment_model(model_id)

    def submit_experiment_prediction(
        self,
        run_id: str,
        trial_id: str,
        prediction: Union[SsvepPrediction, Mapping[str, Any]],
        *,
        model_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not str(run_id).strip() or not str(trial_id).strip():
            raise ValueError("run_id and trial_id are required")
        if isinstance(prediction, SsvepPrediction):
            normalized = prediction
            source_model_id = None
            source_attempt_id = None
        elif isinstance(prediction, Mapping):
            normalized = SsvepPrediction.from_dict(prediction)
            source_model_id = prediction.get("modelId", prediction.get("model_id"))
            source_attempt_id = prediction.get("attemptId", prediction.get("attempt_id"))
        else:
            raise TypeError("prediction must be SsvepPrediction or a mapping")
        resolved_model_id = str(model_id or source_model_id or "").strip()
        if not resolved_model_id:
            raise ValueError("model_id is required")
        resolved_attempt_id = str(attempt_id or source_attempt_id or "").strip()
        if not resolved_attempt_id:
            raise ValueError("attempt_id is required")
        body = normalized.to_dict()
        body["modelId"] = resolved_model_id
        body["attemptId"] = resolved_attempt_id
        encoded_run_id = quote(str(run_id).strip(), safe="")
        encoded_trial_id = quote(str(trial_id).strip(), safe="")
        return self._json(
            "POST",
            f"/v1/experiments/runs/{encoded_run_id}/trials/{encoded_trial_id}/prediction",
            json=body,
        )

    def submit_ssvep_prediction(
        self,
        run_id: str,
        trial_id: str,
        prediction: Union[SsvepPrediction, Mapping[str, Any]],
        *,
        model_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.submit_experiment_prediction(
            run_id,
            trial_id,
            prediction,
            model_id=model_id,
            attempt_id=attempt_id,
        )

    def serve_ssvep_model(
        self,
        name: str,
        predict_fn: Callable[[SsvepTrialWindow], Union[SsvepPrediction, Mapping[str, Any]]],
        version: Optional[str] = None,
        stop_event: Optional[Any] = None,
        heartbeat_interval: float = 5,
    ) -> None:
        """Register and serve a synchronous SSVEP prediction callback."""
        if not callable(predict_fn):
            raise TypeError("predict_fn must be callable")
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be greater than zero")

        registration = self.register_experiment_model(name, version=version, paradigm="ssvep")
        shutdown = threading.Event()
        stop_signal = _CombinedStopEvent(shutdown, stop_event)
        heartbeat_errors: List[BaseException] = []

        def maintain_heartbeat() -> None:
            while not shutdown.wait(float(heartbeat_interval)):
                if stop_event is not None and stop_event.is_set():
                    shutdown.set()
                    return
                try:
                    self.heartbeat_experiment_model(registration.model_id)
                except Exception as exc:  # surfaced on the serving thread
                    heartbeat_errors.append(exc)
                    shutdown.set()
                    return

        heartbeat_thread = threading.Thread(
            target=maintain_heartbeat,
            name=f"xingshu-ssvep-heartbeat-{registration.model_id}",
            daemon=True,
        )
        heartbeat_thread.start()

        def log_disconnect(reason: Optional[BaseException]) -> None:
            logger.warning(
                "SSVEP model %s lost the event stream (%s); reconnecting.",
                registration.model_id,
                reason if reason is not None else "connection closed",
            )

        try:
            for event in self.events(
                types=["experiment.trial.window"],
                timeout=1.0,
                stop_event=stop_signal,
                on_disconnect=log_disconnect,
            ):
                if stop_signal.is_set():
                    break
                payload = event.payload
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("modelId") != registration.model_id:
                    continue
                # One bad trial (malformed window, predict_fn bug, or a
                # rejected/late submission) must not take the model offline:
                # log it, keep the heartbeat alive, and wait for the next
                # trial window.
                try:
                    trial = SsvepTrialWindow.from_dict(payload)
                except (TypeError, ValueError) as exc:
                    logger.warning("Skipping a malformed SSVEP trial window: %s", exc)
                    continue
                try:
                    predicted = predict_fn(trial)
                    self.submit_experiment_prediction(
                        trial.run_id,
                        trial.trial_id,
                        predicted,
                        model_id=registration.model_id,
                        attempt_id=trial.attempt_id,
                    )
                except Exception:
                    logger.exception(
                        "SSVEP prediction for attempt %s failed; the serving loop continues.",
                        trial.attempt_id,
                    )
                    continue
            if heartbeat_errors:
                raise heartbeat_errors[0]
        finally:
            shutdown.set()
            heartbeat_thread.join(timeout=max(1.0, min(float(heartbeat_interval) + 1.0, 10.0)))
            try:
                self.unregister_experiment_model(registration.model_id)
            except XingShuError:
                pass

    # ── WebSocket events ───────────────────────────────────────────────

    def _connect_events_ws(
        self,
        types: Optional[Sequence[str]],
        *,
        timeout: Optional[float],
        token_in_query: bool,
    ) -> Any:
        """Open one authenticated WebSocket connection to ``/v1/events``.

        The token travels in the ``Authorization: Bearer`` header by default
        so it never appears in URLs or request logs. ``token_in_query=True``
        keeps the legacy ``?token=`` behaviour (URL-encoded) for servers that
        cannot forward upgrade headers.
        """
        params: List[str] = []
        connect_options: Dict[str, Any] = {}
        if token_in_query:
            params.append("token=" + quote(self.token, safe=""))
        else:
            connect_options["header"] = [f"Authorization: Bearer {self.token}"]
        if types:
            params.append("events=" + quote(",".join(str(item) for item in types), safe=","))
        url = self.ws_url
        full_url = f"{url}{'&' if '?' in url else '?'}{'&'.join(params)}" if params else url
        direct_socket = None
        try:
            parsed_url = urlparse(full_url)
            if not self.trust_env and parsed_url.scheme == "ws":
                host = parsed_url.hostname
                if not host:
                    raise ValueError("WebSocket URL is missing a host")
                direct_socket = socket.create_connection(
                    (host, parsed_url.port or 80),
                    timeout=timeout,
                )
                connect_options["socket"] = direct_socket
            return websocket.create_connection(full_url, timeout=timeout, **connect_options)
        except Exception as exc:
            if direct_socket is not None:
                try:
                    direct_socket.close()
                except Exception:
                    pass
            raise XingShuConnectionError(str(exc), code="WS_CONNECT_FAILED") from exc

    @staticmethod
    def _wait_before_reconnect(delay: float, stop_event: Optional[Any]) -> None:
        deadline = time.monotonic() + max(0.0, float(delay))
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def events(
        self,
        types: Optional[Sequence[str]] = None,
        *,
        include_heartbeat: bool = False,
        timeout: Optional[float] = None,
        stop_event: Optional[Any] = None,
        reconnect: bool = True,
        max_reconnect_attempts: Optional[int] = 5,
        reconnect_initial_delay_s: float = 0.5,
        reconnect_max_delay_s: float = 30.0,
        on_disconnect: Optional[Callable[[Optional[BaseException]], None]] = None,
        token_in_query: bool = False,
    ) -> Iterator[Event]:
        """Subscribe to the WebSocket event stream.

        Yields :class:`Event` instances. Filtering by ``types`` is evaluated
        server-side via the ``?events=`` query param. Authentication uses the
        ``Authorization: Bearer`` header; set ``token_in_query=True`` only for
        servers that cannot read upgrade headers (the token is then URL-encoded
        into ``?token=``).

        The very first connection failure always raises
        :class:`XingShuConnectionError`. Once a connection has been
        established, a dropped connection is re-established automatically with
        exponential backoff (``reconnect_initial_delay_s`` doubling up to
        ``reconnect_max_delay_s``) while ``reconnect`` is true.
        ``on_disconnect`` is invoked with the causing exception (or ``None``
        for a clean close) before each reconnect attempt. After
        ``max_reconnect_attempts`` consecutive failed reconnects the iterator
        raises ``XingShuConnectionError(code="WS_RECONNECT_EXHAUSTED")``;
        pass ``None`` to retry forever. Event ``seq`` numbering restarts on
        every reconnect and events published while disconnected are lost.

        Set ``stop_event`` to a threading-compatible event to stop the
        subscription (including backoff waits) without closing the client.
        """
        if stop_event is not None and timeout is None:
            # Without a receive timeout, ws.recv() would block indefinitely
            # and a set stop_event would never be observed.
            timeout = 1.0
        connected_once = False
        reconnect_failures = 0
        pending_reconnect = False
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            if pending_reconnect:
                delay = min(
                    float(reconnect_initial_delay_s) * (2.0 ** reconnect_failures),
                    float(reconnect_max_delay_s),
                )
                self._wait_before_reconnect(delay, stop_event)
                if stop_event is not None and stop_event.is_set():
                    return
            try:
                ws = self._connect_events_ws(types, timeout=timeout, token_in_query=token_in_query)
            except XingShuConnectionError:
                if not connected_once or not reconnect:
                    raise
                reconnect_failures += 1
                if max_reconnect_attempts is not None and reconnect_failures >= int(max_reconnect_attempts):
                    raise XingShuConnectionError(
                        f"WebSocket reconnection failed {reconnect_failures} times in a row",
                        code="WS_RECONNECT_EXHAUSTED",
                    )
                continue
            connected_once = True
            reconnect_failures = 0
            disconnect_cause: Optional[BaseException] = None
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        return
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except (websocket.WebSocketException, OSError) as exc:
                        disconnect_cause = exc
                        break
                    if not raw:
                        break
                    try:
                        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else None
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    event = Event.from_dict(payload)
                    if event.type == "heartbeat" and not include_heartbeat:
                        continue
                    yield event
            finally:
                try:
                    ws.close()
                except Exception:  # pragma: no cover
                    pass
            if stop_event is not None and stop_event.is_set():
                return
            if not reconnect:
                return
            if on_disconnect is not None:
                try:
                    on_disconnect(disconnect_cause)
                except Exception:  # pragma: no cover - user callback safety
                    logger.exception("The events() on_disconnect callback raised")
            pending_reconnect = True
