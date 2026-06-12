from __future__ import annotations

import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import MethodNotAllowed, NotFound

from db import DbError, _UNSET
from logging_setup import get_logger

log = get_logger("orch.web")

_FRONTEND = Path(__file__).resolve().parent.parent / "FRONTEND"


def create_app(orchestrator) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(_FRONTEND / "templates"),
        static_folder=str(_FRONTEND / "static"),
        static_url_path="/static",
    )

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")

    @app.post("/api/v1/logs")
    def client_log():
        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"error": {"code": "bad_request", "message": "message is required"}}), 400
        from logging_setup import write_frontend_log

        write_frontend_log(
            level=body.get("level") or "info",
            message=message,
            context=body.get("context") if isinstance(body.get("context"), dict) else None,
        )
        return ("", 204)

    @app.get("/api/v1/model-setup")
    def model_setup_status():
        return jsonify(orchestrator.model_setup_status())

    @app.get("/api/v1/models")
    def list_models():
        scope = (request.args.get("scope") or "configured").strip().lower()
        return jsonify(orchestrator.list_available_models(scope=scope))

    @app.post("/api/v1/model-setup/test")
    def model_setup_test():
        body = request.get_json(silent=True) or {}
        prompt = (body.get("prompt") or "").strip()
        model = body.get("model")
        if model is not None:
            model = str(model).strip() or None
        if not prompt:
            return jsonify({"error": {"code": "bad_request", "message": "prompt is required"}}), 400
        try:
            result = orchestrator.test_model_setup(prompt, model=model)
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        if result.get("error"):
            return jsonify({"error": {"code": "model_error", "message": result["error"]}}), 502
        return jsonify({"reply": result.get("reply", "")})

    @app.post("/api/v1/model-setup/confirm")
    def model_setup_confirm():
        body = request.get_json(silent=True) or {}
        model = body.get("model")
        if model is not None:
            model = str(model).strip() or None
        return jsonify(orchestrator.confirm_model_setup(model))

    @app.post("/api/v1/model-setup/logout")
    def model_setup_logout():
        return jsonify(orchestrator.logout_model_setup())

    @app.get("/api/v1/state")
    def state():
        project = (request.args.get("project") or "").strip() or None
        return jsonify(orchestrator.api_state(project_slug=project))

    @app.post("/api/v1/refresh")
    def refresh():
        orchestrator.request_refresh()
        from datetime import datetime, timezone

        return (
            jsonify(
                {
                    "queued": True,
                    "coalesced": False,
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                    "operations": ["poll", "reconcile"],
                }
            ),
            202,
        )

    # --- built-in tracker: projects & tasks ---
    @app.get("/api/v1/projects")
    def list_projects():
        return jsonify({"projects": orchestrator.db.list_projects()})

    @app.post("/api/v1/projects")
    def create_project():
        body = request.get_json(silent=True) or {}
        try:
            project = orchestrator.db.create_project(
                title=body.get("title", ""),
                description=body.get("description"),
                slug=body.get("slug"),
                needs_git=bool(body.get("needs_git")),
                workspace_path=body.get("workspace_path"),
            )
        except DbError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(project), 201

    @app.patch("/api/v1/projects/<slug>")
    def update_project(slug: str):
        body = request.get_json(silent=True) or {}
        needs_git = bool(body["needs_git"]) if "needs_git" in body else None
        workspace_path = body.get("workspace_path") if "workspace_path" in body else _UNSET
        try:
            project = orchestrator.db.update_project(
                slug,
                title=body.get("title", ""),
                description=body.get("description"),
                needs_git=needs_git,
                workspace_path=workspace_path,
            )
        except DbError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(project)

    @app.get("/api/v1/fs/directories")
    def browse_directories():
        path = request.args.get("path")
        try:
            data = orchestrator.browse_directories(path)
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(data)

    @app.delete("/api/v1/projects/<slug>")
    def delete_project(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        # If we're actively orchestrating this project, stop first to avoid races.
        if orchestrator.active_project_slug == slug and orchestrator.orchestrating:
            orchestrator.pause_orchestration()
        try:
            orchestrator.db.delete_project(slug)
        except DbError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        orchestrator.remove_project_workspace(slug)
        return jsonify({"deleted": slug})

    @app.get("/api/v1/projects/<slug>/board")
    def project_board(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        iteration_id = (request.args.get("iteration") or "").strip() or None
        iteration = None
        if iteration_id:
            iteration = orchestrator.db.get_iteration(iteration_id)
            if not iteration or iteration.get("project_slug") != slug:
                return jsonify({"error": {"code": "iteration_not_found", "message": iteration_id}}), 404
        return jsonify(
            {
                "project_slug": slug,
                "iteration_id": iteration_id,
                "iteration": iteration,
                "columns": orchestrator.db.board(slug, iteration_id=iteration_id),
            }
        )

    @app.get("/api/v1/projects/<slug>/iterations")
    def list_iterations(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        return jsonify({"project_slug": slug, "iterations": orchestrator.db.list_iterations(slug)})

    @app.post("/api/v1/projects/<slug>/iterations")
    def create_iteration(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        body = request.get_json(silent=True) or {}
        title = (body.get("title") or "").strip()
        try:
            iteration = orchestrator.create_iteration(slug, title or "New iteration")
        except DbError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(iteration), 201

    @app.patch("/api/v1/projects/<slug>/iterations/<iteration_id>")
    def update_iteration(slug: str, iteration_id: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        iteration = orchestrator.db.get_iteration(iteration_id)
        if not iteration or iteration.get("project_slug") != slug:
            return jsonify({"error": {"code": "iteration_not_found", "message": iteration_id}}), 404
        body = request.get_json(silent=True) or {}
        try:
            updated = orchestrator.update_iteration(
                slug,
                iteration_id,
                testing_instructions=body.get("testing_instructions"),
            )
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(updated)

    @app.post("/api/v1/projects/<slug>/iterations/<iteration_id>/rerun")
    def rerun_iteration(slug: str, iteration_id: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        iteration = orchestrator.db.get_iteration(iteration_id)
        if not iteration or iteration.get("project_slug") != slug:
            return jsonify({"error": {"code": "iteration_not_found", "message": iteration_id}}), 404
        try:
            orchestrator.rerun_iteration(slug, iteration_id)
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(
            {"orchestrating": True, "project_slug": slug, "iteration_id": iteration_id}
        ), 202

    @app.post("/api/v1/projects/<slug>/tasks")
    def create_task(slug: str):
        body = request.get_json(silent=True) or {}
        priority = body.get("priority")
        try:
            priority = int(priority) if priority not in (None, "") else None
        except (TypeError, ValueError):
            priority = None
        iteration_id = (body.get("iteration_id") or "").strip()
        kind = (body.get("kind") or "task").strip().lower()
        if not iteration_id:
            return jsonify({"error": {"code": "bad_request", "message": "iteration_id is required"}}), 400
        try:
            task = orchestrator.create_task(
                project_slug=slug,
                iteration_id=iteration_id,
                title=body.get("title", ""),
                description=body.get("description"),
                priority=priority,
                kind=kind,
            )
        except DbError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(task), 201

    @app.patch("/api/v1/projects/<slug>/tasks/<task_id>")
    def update_task(slug: str, task_id: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        existing = orchestrator.db.get_task(task_id)
        if not existing or existing.get("project_slug") != slug:
            return jsonify({"error": {"code": "task_not_found", "message": task_id}}), 404
        body = request.get_json(silent=True) or {}
        kwargs = {}
        if "state" in body:
            kwargs["state"] = body.get("state")
        if "priority" in body:
            raw = body.get("priority")
            try:
                kwargs["priority"] = int(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                kwargs["priority"] = None
        if "title" in body:
            kwargs["title"] = body.get("title", "")
        if "description" in body:
            kwargs["description"] = body.get("description")
        try:
            task = orchestrator.db.update_task(task_id, **kwargs)
        except DbError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(task)

    @app.post("/api/v1/projects/<slug>/tasks/<task_id>/copy-downstream")
    def copy_task_downstream(slug: str, task_id: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        existing = orchestrator.db.get_task(task_id)
        if not existing or existing.get("project_slug") != slug:
            return jsonify({"error": {"code": "task_not_found", "message": task_id}}), 404
        try:
            result = orchestrator.copy_task_downstream(slug, task_id)
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(result), 201

    @app.delete("/api/v1/projects/<slug>/tasks/<task_id>")
    def delete_task(slug: str, task_id: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        try:
            orchestrator.delete_task(slug, task_id)
        except ValueError as exc:
            return jsonify({"error": {"code": "task_not_found", "message": str(exc)}}), 404
        return ("", 204)

    _MAX_UPLOAD_BYTES = 25 * 1024 * 1024

    @app.post("/api/v1/projects/<slug>/tasks/<task_id>/upload")
    def upload_task_file(slug: str, task_id: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        task = orchestrator.db.get_task(task_id)
        if not task or task.get("project_slug") != slug:
            return jsonify({"error": {"code": "task_not_found", "message": task_id}}), 404
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": {"code": "bad_request", "message": "file is required"}}), 400
        data = upload.read()
        if len(data) > _MAX_UPLOAD_BYTES:
            return jsonify({"error": {"code": "too_large", "message": "file exceeds 25 MB"}}), 400
        try:
            rel = orchestrator.save_task_upload(slug, task["identifier"], upload.filename, data)
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify({"saved": rel, "size": len(data)}), 201

    # --- artifacts: files agents produced under WORKSPACES/<slug>/ ---
    @app.get("/api/v1/projects/<slug>/files")
    def project_files(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        return jsonify({"project_slug": slug, "files": orchestrator.list_project_files(slug)})

    @app.get("/api/v1/projects/<slug>/file")
    def project_file(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        rel = request.args.get("path", "")
        if not rel:
            return jsonify({"error": {"code": "bad_request", "message": "path is required"}}), 400
        try:
            data = orchestrator.read_project_file(slug, rel)
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        if data is None:
            return jsonify({"error": {"code": "file_not_found", "message": rel}}), 404
        return jsonify(data)

    @app.put("/api/v1/projects/<slug>/file")
    def save_project_file(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        body = request.get_json(silent=True) or {}
        rel = (body.get("path") or "").strip()
        if not rel:
            return jsonify({"error": {"code": "bad_request", "message": "path is required"}}), 400
        if "content" not in body:
            return jsonify({"error": {"code": "bad_request", "message": "content is required"}}), 400
        try:
            data = orchestrator.write_project_file(slug, rel, body.get("content") or "")
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(data)

    @app.post("/api/v1/projects/<slug>/file")
    def create_project_file(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        body = request.get_json(silent=True) or {}
        rel = (body.get("path") or "").strip()
        if not rel:
            return jsonify({"error": {"code": "bad_request", "message": "path is required"}}), 400
        if "content" not in body:
            return jsonify({"error": {"code": "bad_request", "message": "content is required"}}), 400
        try:
            data = orchestrator.create_project_file(slug, rel, body.get("content") or "")
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(data), 201

    @app.post("/api/v1/projects/<slug>/console")
    def project_console(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        body = request.get_json(silent=True) or {}
        command = (body.get("command") or "").strip()
        if not command:
            return jsonify({"error": {"code": "bad_request", "message": "command is required"}}), 400
        try:
            result = orchestrator.run_workspace_command(slug, command)
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify(result)

    # --- runs (orchestrator data: tokens + runtime per attempt) ---
    @app.get("/api/v1/runs")
    def list_runs():
        limit = request.args.get("limit", 50, type=int)
        return jsonify({"runs": orchestrator.db.list_runs(limit=limit)})

    @app.get("/api/v1/projects/<slug>/runs")
    def project_runs(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        limit = request.args.get("limit", 50, type=int)
        return jsonify({"project_slug": slug, "runs": orchestrator.db.list_runs(slug, limit)})

    # --- git history ---
    @app.get("/api/v1/projects/<slug>/commits")
    def project_commits(slug: str):
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        limit = request.args.get("limit", 100, type=int)
        return jsonify({"project_slug": slug, "commits": orchestrator.list_commits(slug, limit)})

    # --- orchestration control ---
    @app.post("/api/v1/orchestrate")
    def orchestrate():
        body = request.get_json(silent=True) or {}
        slug = (body.get("project_slug") or "").strip()
        if not slug:
            return jsonify({"error": {"code": "bad_request", "message": "project_slug is required"}}), 400
        if not orchestrator.db.get_project(slug):
            return jsonify({"error": {"code": "project_not_found", "message": slug}}), 404
        iteration_id = (body.get("iteration_id") or "").strip()
        if not iteration_id:
            return jsonify({"error": {"code": "bad_request", "message": "iteration_id is required"}}), 400
        try:
            if bool(body.get("plan")):
                orchestrator.plan_and_orchestrate(slug, iteration_id)
                return jsonify(
                    {
                        "orchestrating": True,
                        "planning": True,
                        "project_slug": slug,
                        "iteration_id": iteration_id,
                    }
                ), 202
            orchestrator.orchestrate(slug, iteration_id)
        except ValueError as exc:
            return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400
        return jsonify({"orchestrating": True, "project_slug": slug, "iteration_id": iteration_id}), 202

    @app.post("/api/v1/stop")
    def stop_orchestration():
        outcome = orchestrator.pause_orchestration()
        return jsonify(outcome), 200

    @app.get("/api/v1/<identifier>")
    def issue(identifier: str):
        data = orchestrator.api_issue(identifier)
        if data is None:
            return jsonify({"error": {"code": "issue_not_found", "message": identifier}}), 404
        return jsonify(data)

    @app.errorhandler(NotFound)
    def _not_found(_exc):
        return jsonify({"error": {"code": "not_found", "message": "no such route"}}), 404

    @app.errorhandler(MethodNotAllowed)
    def _method_not_allowed(_exc):
        return jsonify({"error": {"code": "method_not_allowed", "message": "unsupported method"}}), 405

    return app


def start_server(orchestrator, host: str, port: int) -> None:
    from werkzeug.serving import make_server

    app = create_app(orchestrator)
    server = make_server(host, port, app, threaded=True)

    def run():
        server.serve_forever()

    thread = threading.Thread(target=run, daemon=True, name="dashboard")
    thread.start()
    log.info("dashboard listening on http://%s:%s", host, port)
