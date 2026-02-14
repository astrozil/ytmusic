import time
import uuid

from flask import g, jsonify, request
from werkzeug.exceptions import HTTPException, InternalServerError


def register_request_hooks(app, logger):
    @app.before_request
    def attach_request_context():
        request_id = request.headers.get("X-Request-ID")
        g.request_id = request_id if request_id else str(uuid.uuid4())
        g.request_started_at = time.perf_counter()

    @app.after_request
    def add_request_headers(response):
        request_id = getattr(g, "request_id", None)
        if request_id:
            response.headers["X-Request-ID"] = request_id

        started_at = getattr(g, "request_started_at", None)
        if started_at is not None:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info(
                "request_id=%s method=%s path=%s status=%s latency_ms=%s",
                request_id,
                request.method,
                request.path,
                response.status_code,
                elapsed_ms,
            )
        return response


def register_error_handlers(app, logger):
    @app.errorhandler(HTTPException)
    def handle_http_exception(error):
        return jsonify({"error": str(error)}), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_exception(error):
        request_id = getattr(g, "request_id", "unknown")
        logger.exception("Unhandled exception request_id=%s: %s", request_id, error)
        internal = InternalServerError(description="An error occurred while processing your request")
        return jsonify({"error": str(internal)}), 500
