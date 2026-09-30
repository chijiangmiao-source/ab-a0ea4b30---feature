"""HTTP 入口（仅标准库）。

路由:
  GET  /                     浏览器页面
  GET  /healthz              健康检查 -> {"status":"ok"}
  POST /api/audits           提交审计
       201 首次冻结; 200 同载荷重放; 409 同号不同载荷; 400 载荷非法
  GET  /api/audits/<id>      按审计编号读取冻结结果; 404 不存在
  POST /api/envelopes        基于可行审计创建/重放运行包线
       201 首次冻结; 200 同规格重放;
       404 来源不存在; 422 来源不可行/超受理规模;
       409 复用编号但改变来源或系数; 400 载荷非法
  GET  /api/envelopes/<id>   读取冻结包线; 404 不存在
  POST /api/envelopes/<id>/query   包线内有理坐标查询
       200 可行给电流/余量，或包线外给首条违约边界; 400 坐标非法; 404 包线不存在
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .envelope_models import EnvelopePayloadError
from .envelope_service import EnvelopeRejectedError, EnvelopeService
from .models import PayloadError
from .service import AuditService, IdConflictError
from .storage import AuditStore, EnvelopeConflictError, EnvelopeStore

_DATA_DIR = os.environ.get("AUDIT_DATA_DIR", "/data")
_HOST = os.environ.get("AUDIT_HOST", "0.0.0.0")
_PORT = int(os.environ.get("AUDIT_PORT", "8080"))
_MAX_BODY = 4 * 1024 * 1024

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "static", "index.html"), "r", encoding="utf-8") as f:
    INDEX_HTML = f.read()


class Handler(BaseHTTPRequestHandler):
    service: AuditService = None  # 由 make_server 注入（类属性）
    envelope_service: EnvelopeService = None

    def log_message(self, fmt, *args):  # 静默默认访问日志，健康检查不刷屏
        if os.environ.get("AUDIT_ACCESS_LOG"):
            super().log_message(fmt, *args)

    # ---- 工具 ----
    def _send_json(self, status: int, obj: dict, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_html(HTTPStatus.OK, INDEX_HTML)
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "coil-audit"})
        elif path.startswith("/api/audits/"):
            audit_id = path[len("/api/audits/"):]
            if not audit_id or "/" in audit_id:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            record = self.service.fetch(audit_id)
            if record is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "not_found", "audit_id": audit_id},
                )
            else:
                self._send_json(HTTPStatus.OK, {"replayed": True, **record})
        elif path.startswith("/api/envelopes/"):
            envelope_id = path[len("/api/envelopes/"):]
            if not envelope_id or "/" in envelope_id:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            record = self.envelope_service.fetch(envelope_id)
            if record is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "not_found", "envelope_id": envelope_id},
                )
            else:
                self._send_json(HTTPStatus.OK, {"replayed": True, **record})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _read_json_body(self):
        """读取并解析 JSON 请求体，返回 (obj, error_response) 二者之一。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > _MAX_BODY:
            return None, (
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_payload", "detail": "请求体缺失或过大"},
            )
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, (
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_payload", "detail": f"JSON 解析失败: {exc}"},
            )

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/audits":
            self._post_audit()
        elif path == "/api/envelopes":
            self._post_envelope()
        elif path.startswith("/api/envelopes/") and path.endswith("/query"):
            envelope_id = path[len("/api/envelopes/"):-len("/query")]
            self._post_envelope_query(envelope_id)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _post_audit(self):
        payload, err = self._read_json_body()
        if err is not None:
            self._send_json(err[0], err[1])
            return
        try:
            record, replayed = self.service.audit(payload)
        except PayloadError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_payload", "detail": str(exc)},
            )
            return
        except IdConflictError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "audit_id_conflict",
                    "detail": "同一审计编号已绑定不同载荷；原证据保持不变",
                    "audit_id": exc.existing["audit_id"],
                    "existing_fingerprint": exc.existing["fingerprint"],
                    "existing_created_at": exc.existing["created_at"],
                    "new_fingerprint": exc.new_fingerprint,
                },
            )
            return
        self._send_json(
            HTTPStatus.OK if replayed else HTTPStatus.CREATED,
            {"replayed": replayed, **record},
        )

    def _post_envelope(self):
        payload, err = self._read_json_body()
        if err is not None:
            self._send_json(err[0], err[1])
            return
        try:
            record, replayed = self.envelope_service.create(payload)
        except EnvelopePayloadError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_payload", "detail": str(exc)},
            )
            return
        except EnvelopeRejectedError as exc:
            status = (
                HTTPStatus.NOT_FOUND
                if exc.code in ("source_not_found", "envelope_not_found")
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
            self._send_json(status, {"error": exc.code, "detail": exc.detail})
            return
        except EnvelopeConflictError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "envelope_id_conflict",
                    "detail": "同一包线编号已绑定不同来源或扰动系数；"
                              "原冻结包线保持不变",
                    "envelope_id": exc.existing["envelope_id"],
                    "existing_fingerprint": exc.existing["fingerprint"],
                    "existing_created_at": exc.existing["created_at"],
                    "new_fingerprint": exc.new_fingerprint},
            )
            return
        self._send_json(
            HTTPStatus.OK if replayed else HTTPStatus.CREATED,
            {"replayed": replayed, **record},
        )

    def _post_envelope_query(self, envelope_id: str):
        if not envelope_id or "/" in envelope_id:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        payload, err = self._read_json_body()
        if err is not None:
            self._send_json(err[0], err[1])
            return
        try:
            answer = self.envelope_service.query(envelope_id, payload)
        except EnvelopePayloadError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_coordinate", "detail": str(exc)},
            )
            return
        except EnvelopeRejectedError as exc:
            status = (
                HTTPStatus.NOT_FOUND
                if exc.code == "envelope_not_found"
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
            self._send_json(status, {"error": exc.code, "detail": exc.detail})
            return
        self._send_json(HTTPStatus.OK, answer)


def make_server(host: str = _HOST, port: int = _PORT, data_dir: str = _DATA_DIR):
    store = AuditStore(data_dir)
    service = AuditService(store)
    envelope_store = EnvelopeStore(data_dir)
    envelope_service = EnvelopeService(store, envelope_store)
    Handler.service = service
    Handler.envelope_service = envelope_service
    return ThreadingHTTPServer((host, port), Handler)


def main():
    server = make_server()
    host, port = server.server_address[:2]
    print(f"coil-audit listening on http://{host}:{port} data={_DATA_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
