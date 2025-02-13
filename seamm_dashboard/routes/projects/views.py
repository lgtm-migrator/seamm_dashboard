from copy import deepcopy
import os

from flask import render_template, url_for, request, flash, redirect
from flask_jwt_extended import jwt_required, get_current_user

from sqlalchemy import func

from pathlib import Path

from wtforms import BooleanField

from . import projects
from .forms import EditProject, ManageProjectAccessForm, AddProject

from seamm_datastore.database.models import (
    Project,
    User,
    UserProjectAssociation,
    Job,
)  # noqa: E501

from seamm_dashboard import authorize, db, datastore

from seamm_dashboard.routes.api.auth import refresh_expiring_jwts


def _bind_users_to_form(form, current_user, project_id):
    """Function to bind current usernames to form

    This has to be done dynamically based on what is in the database. That's why it is
    in this file and occurs when the page is viewed rather than in the form object.
    """

    actions = ["read", "update", "create", "delete", "manage"]
    users = User.query.all()
    user_names = []

    if len(users) > 1:
        # Put current user first
        users.remove(current_user)
        reorder_users = [current_user]
        reorder_users.extend(users)
        users = reorder_users

    for user in users:
        field_name = f"user_{user.id}"
        permissions = []
        assoc = UserProjectAssociation.query.filter_by(
            resource_id=project_id, entity_id=user.id
        ).one_or_none()

        if assoc:
            permissions = assoc.permissions

        for action in actions:
            checked = action in permissions
            setattr(form, f"{field_name}_{action}", BooleanField(default=checked))

        user_names.append({"username": user.username, "id": user.id})

    return form, user_names


@projects.route("/views/projects")
@jwt_required(optional=True)
def project_list():
    return render_template("projects/project_list.html")


@projects.route("/views/projects/<id>/jobs")
@projects.route("/views//projects/<id>/jobs")
@jwt_required(optional=True)
def project_jobs_list(id):

    project = Project.query.get(id)

    manage_project = authorize.manage(project)
    edit_project = authorize.update(project)

    # Build the url ourselves.
    base_url = url_for("main.index")
    edit_url = base_url + f"projects/{id}/edit"
    manage_url = base_url + f"projects/{id}/manage"

    return render_template(
        "jobs/jobs_list.html",
        project=True,
        manage_project=manage_project,
        edit_project=edit_project,
        edit_url=edit_url,
        manage_url=manage_url,
    )


@projects.route("/projects/<project_id>/edit", methods=["GET", "POST"])
@jwt_required(optional=True)
def edit_project(project_id):

    project = Project.query.get(project_id)

    if not authorize.update(project):
        return render_template("401.html")

    form = EditProject()

    # Build the url ourselves.
    base_url = url_for("main.index")
    project_url = base_url + f"#projects/{project_id}/jobs"

    if form.validate_on_submit():
        # Rename project path
        path = project.path
        new_path = os.path.join(os.path.dirname(path), form.name.data)

        # Make sure project name doesn't exist
        exists = Project.query.filter(Project.name == form.name.data).one_or_none()
        if exists is not None:
            flash(f"A project with the name {form.name.data} already exists.")
            return redirect(url_for("projects.edit_project", project_id=project_id))

        # Need to change this to query, but this will work
        # without modifying the DB for now.
        job_ids = [x.id for x in project.jobs]

        # Update job paths
        Job.query.filter(Job.id.in_(job_ids)).update(
            {Job.path: func.replace(Job.path, path, new_path)},
            synchronize_session=False,
        )

        # Perform rename
        os.rename(path, new_path)

        # Update project information
        project.path = new_path
        project.name = form.name.data
        project.description = form.notes.data
        db.session.commit()
        flash("Project updated successfully.", "successs")

        return redirect(project_url)

    elif request.method == "GET":
        form.name.data = project.name
        form.notes.data = project.description

    return render_template(
        "jobs/edit_job.html",
        title=f"Edit Project {project_id}",
        form=form,
        back_url=project_url,
    )


@projects.route("/projects/<project_id>/manage", methods=["GET", "POST"])
@jwt_required(optional=True)
def manage_project(project_id):

    project = Project.query.get(project_id)

    if not project:
        return render_template("404.html")

    if not authorize.manage(project):
        return render_template("401.html")

    form = deepcopy(ManageProjectAccessForm)

    # Add users to form
    form, usernames = _bind_users_to_form(
        form, current_user=get_current_user(), project_id=project.id
    )

    # Add public permissions information to form
    public = "read" in project.permissions["other"]
    form.allow_public = BooleanField("Make project publicly readable", default=public)

    form = form()

    # Build the url ourselves.
    base_url = url_for("main.index")
    project_url = base_url + f"#projects/{project_id}/jobs"

    if request.method == "POST":
        if form.validate_on_submit():

            if form.allow_public:
                permissions = deepcopy(project.permissions)
                permissions["other"] = ["read"]
                project.permissions = permissions
                db.session.commit()

            user_keys = [
                x for x in form.data.keys() if "user" in x if form.data[x] is True
            ]

            permissions_dict = {}

            for key in user_keys:
                split = key.split("_")
                user_id = int(split[1])
                permission = split[2]

                try:
                    permissions_dict[user_id].append(permission)
                except KeyError:
                    permissions_dict[user_id] = [permission]

            # Find an entries for special user permissions which exist for this project
            users = User.query.all()

            for entry in users:
                user_id = entry.id

                try:
                    permissions = permissions_dict[user_id]
                except KeyError:
                    permissions = []

                assoc = UserProjectAssociation.query.filter_by(
                    entity_id=user_id, resource_id=project_id
                ).one_or_none()

                if not assoc:
                    assoc = UserProjectAssociation(
                        entity_id=user_id,
                        resource_id=project_id,
                        permissions=permissions,
                    )
                else:
                    assoc.permissions = permissions

                db.session.add(assoc)
                db.session.commit()

            flash(f"Permissions for {project.name} successfully updated.")
            return redirect(project_url)

    return render_template(
        "projects/project_access.html",
        form=form,
        users=usernames,
        project=project,
        back_url=project_url,
    )


@projects.route("/projects/add", methods=["GET", "POST"])
@jwt_required(optional=True)
def add_project():

    if get_current_user() is None:
        return render_template("404.html")

    form = AddProject()

    # Build the url ourselves.
    base_url = url_for("main.index")
    project_url = base_url + "#projects"

    if form.validate_on_submit():
        # Create a directory for the project
        project_path = Path(datastore).expanduser() / "projects" / form.name.data

        project_path.mkdir(parents=True, exist_ok=True)
        project = Project.create(
            name=form.name.data, description=form.notes.data, path=str(project_path)
        )
        db.session.add(project)
        db.session.commit()
        flash(f"Project {project.name} added.", "successs")

        return redirect(project_url)

    return render_template(
        "jobs/edit_job.html",
        title="Add project",
        form=form,
        back_url=project_url,
    )


@projects.after_request
def project_refresh(response):
    return refresh_expiring_jwts(response)
