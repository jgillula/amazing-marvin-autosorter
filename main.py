#!/usr/bin/env python3

import os
import json
import time

# We use flask to handle the HTTP requests
# https://flask.palletsprojects.com/en/2.0.x/
from flask import Flask, request, send_from_directory, abort

# We use flask_cors because Amazing Marvin requires CORS
# https://github.com/amazingmarvin/MarvinAPI/wiki/Webhooks#technical-details
# https://flask-cors.readthedocs.io/en/latest/
from flask_cors import CORS

# We use flask_httpauth to restrict access to clients providing the correct HTTP token
# https://flask-httpauth.readthedocs.io/en/latest/index.html
# https://developer.mozilla.org/en-US/docs/Web/HTTP/Authentication
from flask_httpauth import HTTPTokenAuth

# We use couchdb to access Amazing Marvin's DB
# https://github.com/amazingmarvin/MarvinAPI/wiki/Database-Access
# https://couchdb-python.readthedocs.io/en/latest/
# https://cloud.ibm.com/apidocs/cloudant?code=python
# https://cloud.ibm.com/docs/Cloudant?topic=Cloudant-query#selector-syntax
import couchdb


app = Flask(__name__)
CORS(app)
auth = HTTPTokenAuth(scheme='Bearer')


# First we load a bunch of settings from the environment
ACCESS_TOKENS = json.loads(os.environ.get("ACCESS_TOKENS_LIST", '[]'))
COUCHDB_USERNAME = os.environ.get("COUCHDB_USERNAME", "")
COUCHDB_PASSWORD = os.environ.get("COUCHDB_PASSWORD", "")
COUCHDB_HOSTNAME = os.environ.get("COUCHDB_HOSTNAME", "")
COUCHDB_DATABASE = os.environ.get("COUCHDB_DATABASE", "")
sort_list = json.loads(os.environ.get("SORT_LIST", '[]'))
UPDATE_TIMEOUT_MILLISECONDS = os.environ.get("UPDATE_TIMEOUT_MILLISECONDS", 5000)
time_between_queries = 1/os.environ.get("QPS_RATE_LIMIT", 1)


LAST_QUERY_TIME = 0
def rate_limit(func):
    """This function lets us globally rate limit calls to the Amazing Marvin backend database

    Args:
        func (function): The function to call once the rate limit expires

    """
    global LAST_QUERY_TIME
    time.sleep(max(0, LAST_QUERY_TIME + time_between_queries - time.time()))
    LAST_QUERY_TIME = time.time()
    return func()


# Setup the database, and initialize the local copy of the database
DB = None
LAST_SEQ = ""
LOCAL_DB = {}
if COUCHDB_USERNAME and COUCHDB_PASSWORD and COUCHDB_HOSTNAME and COUCHDB_DATABASE:
    server = couchdb.Server(f"https://{COUCHDB_USERNAME}:{COUCHDB_PASSWORD}@{COUCHDB_HOSTNAME}")
    DB = server[COUCHDB_DATABASE]
    LAST_SEQ = rate_limit(lambda: DB.info().get('update_seq'))
    LOCAL_DB = dict([(item.id, item) for item in rate_limit(lambda: DB.find({"selector": {"$or": [{"db": "Tasks"}, {"db": "Categories"}]}}))])


def update_db(changes):
    """Update our LOCAL_DB given a list of changes that returned from db.changes(...)

    Args:
        changes: A list of changes that returned from couchdb.Database.changes()
    """
    # global LOCAL_DB
    LOCAL_DB.update([(item.id, item) for item in rate_limit(lambda: DB.find({"selector": {"$or": [{"_id": id} for id in set([change['id'] for change in changes if not change.get('deleted', False)])]}})) if item['db']=="Tasks" or item["db"]=="Categories"])
    for id_to_delete in set([change['id'] for change in changes if change.get("deleted", False) and change['id'] in LOCAL_DB]):
        del LOCAL_DB[id_to_delete]


def label_key(task, options):
    """Comparison key for comparing tasks by their label

    Args:
        task: The task to generate the comparison key for
        options: The options to specify how to generate the key
    """
    for label_title in options["labels"]:
        if label_titles_to_ids[label_title] in task.get('labelIds', []):
            return options["labels"].index(label_title)
    if options.get("no_match_last", True):
        return len(options["labels"])
    return -1


label_titles_to_ids = {}
def label_key_factory(options):
    """A factory to generate label key comparison functions

    Args:
        options: The options to specify how to generate the key
    """
    global label_titles_to_ids
    if not label_titles_to_ids:
        result = rate_limit(lambda: DB.get("strategySettings.labels"))
        if result:
            label_titles_to_ids = dict([(label['title'], label['_id']) for label in result.get('val', [])])
    return lambda task: label_key(task, options)


def is_ready(task):
    """Key to compare tasks on whether or not they're ready

    Args:
        task: The task to generate the comparison key for
    """
    ready = (task.get('backburner', False) is False)
    dependsOn = task.get('dependsOn', {})
    if isinstance(dependsOn, dict):
        ready = (ready and all([LOCAL_DB.get(key, {"done": True}).get('done', False) for key in dependsOn.keys()]))
    return ready


sort_functions = {"field": lambda options: (lambda task: options["empty_value"] if task.get(options["field_name"], options["empty_value"]) is None and options.get("replace_none_with_empty", True) else task.get(options["field_name"], options["empty_value"]),
                                            options.get("reverse", False),
                                            [options["field_name"]]),
                  "label": lambda options: (label_key_factory(options),
                                            options.get("reverse", False),
                                            ["labelIds"]),
                  "is_ready": lambda options: (lambda task: is_ready(task),
                                               options.get("reverse", False),
                                               ["done", "dependsOn", "backburner"])}
sort_list.append(("field", {"field_name": "masterRank", "empty_value": 0}))
processed_sort_list = []
relevant_fields = set(["parentId"])
for sort in reversed(sort_list):
    key, reverse, fields = sort_functions[sort[0]](sort[1])
    processed_sort_list.append((key, reverse))
    relevant_fields.update(fields)


def sort_and_update_by_task(edited_task, db_updated, sort_old_project=False, sort_dependencies=False):
    """Sort and update all tasks affected by a single task

    Args:
        edited_task: The task that changed
        db_updated (function): A function that takes the edited_task and a set of changes and returns True if the edit is reflected in the changes or LOCAL_DB
        sort_old_project (bool): Whether or not to sort the old project of edited_task
        sort_dependencies (bool): Whether or not to sort the dependencies of edited_task
    """
    # First we check that the edited_task has all the fields we're going to need
    if not isinstance(edited_task, dict) or not all([field in edited_task.keys() for field in ["_id", "parentId"]]):
        return {"success": False, "message": "edited_task wasn't a dict or didn't have the required fields"}

    # start by including the current project in the list of tasks to sort
    parent_ids = set([edited_task["parentId"]])

    if sort_old_project:
        # This gets the parentId of the task from the list of previous revisions (assuming the parentId wasn't "unassigned", which means inbox)
        old_parent_id = LOCAL_DB[edited_task["_id"]]['parentId']
        if old_parent_id != "unassigned":
            parent_ids.update(old_parent_id)

    # Next we wait until the database is updated
    changes = []
    global LAST_SEQ
    if not db_updated(edited_task, changes):
        change_feed = rate_limit(lambda: DB.changes(since=LAST_SEQ, feed="continuous", timeout=UPDATE_TIMEOUT_MILLISECONDS))
        for change in change_feed:
            if 'last_seq' in change:
                LAST_SEQ = change['last_seq']
            else:
                changes.append(change)
                if db_updated(edited_task, changes):
                    LAST_SEQ = change['seq']
                    break
        global LAST_QUERY_TIME
        LAST_QUERY_TIME = time.time()
        # Download the changes and update our db
        update_db(changes)
    # If we're still waiting for the database to be updated, return failure
    if not db_updated(edited_task, changes):
        return {"success": False, "message": f"timed out waiting for deletion, LAST_SEQ = {LAST_SEQ}"}

    if sort_dependencies:
        # This crazy one-liner uses a list comprehension to get all parentIds of tasks for which the edited_task's id is in the task's dependsOn list
        parent_ids.update([task['parentId'] for task in LOCAL_DB.values() if edited_task["_id"] in (task.get('dependsOn', {}) if task.get('dependsOn') is not None else {}).keys()])

    return sort_and_update_by_parent_ids(parent_ids)


def sort_and_update_by_parent_ids(parent_ids):
    """This gets all the tasks in all the projects in parent_ids

    Args:
        parent_ids (list): The list of parent_ids whose child tasks you want to sort

    """
    affected_tasks = list([item for item in LOCAL_DB.values() if item['parentId'] in parent_ids])
    # We sort per-project, but will update all the changes at once
    updated_docs = []
    for parent_id in parent_ids:
        project_tasks = [task for task in affected_tasks if task['parentId'] == parent_id]
        # do the sorting
        for key, reverse in processed_sort_list: # pylint: disable=redefined-outer-name
            project_tasks.sort(key=key, reverse=reverse)
        # cycle through  all of the tasks by index
        for i in range(len(project_tasks)):
            # if its old masterRank doesn't equal it's new index+1 (+1 because masterRank starts at 1, not 0)
            append_task = False
            if project_tasks[i].get('masterRank') != i+1:
                project_tasks[i]['masterRank'] = i+1
                append_task = True
            # Or if the note is empty, delete it
            if project_tasks[i].get('note', '') == "\\\n":
                del project_tasks[i]['note']
                append_task = True
            if append_task:
                updated_docs.append(project_tasks[i])
    # Update the changed docs on the server
    result = rate_limit(lambda: DB.update(updated_docs))
    if not all(item[1] for item in result):
        return {"success": False, "message": "Error updating docs in database"}
    return {"success": True, "message": f"Updated {len(updated_docs)}/{len(affected_tasks)} docs, LAST_SEQ = {LAST_SEQ}"}


@auth.verify_token
def verify_token(token):
    """Verify that the access token is valid

    Args:
        token (string): The access token to check

    """
    app.logger.debug(f"Trying to verify with token {token}") # pylint: disable=no-member
    return token in ACCESS_TOKENS


@app.before_request
def check_db_connection():
    """Check if the database connection is established"""
    if not DB:
        return {"success": False, "message": "db connection failed"}
    return None


@app.route('/<string:access_token>/sortAll')
def sortAll(access_token):
    """Re-sort all tasks"""
    global LAST_SEQ
    if verify_token(access_token):
        changes = rate_limit(lambda: DB.changes(since=LAST_SEQ))
        update_db(changes["results"])
        LAST_SEQ = changes["last_seq"]
        parent_ids = [item.id for item in LOCAL_DB.values() if item['db'] == "Categories"]
        result =  sort_and_update_by_parent_ids(parent_ids)
        if result["success"]:
            return send_from_directory(".", "close.html")
        return result
    abort(404)
    return None


@app.route('/edit', methods=['POST'])
@auth.login_required
def edit():
    """Re-sort after a task is edited, if necessary"""
    # global LAST_SEQ
    edited_task = request.get_json(silent=True)
    updated_fields = edited_task.get("setter", {}).keys()
    if any([updated_field in relevant_fields for updated_field in updated_fields]):
        # if done changed, need to sort current project and all tasks which depend on task
        # if parentId changed, need to sort old project and current project
        return sort_and_update_by_task(edited_task,
                                       # If we're waiting for the update, then the new revision should either already be in the LOCAL_DB, or the id and rev should be in the change list
                                       lambda task, changes: task.get("_rev") == LOCAL_DB.get(task["_id"], {}).get("_rev") or (task["_id"], [task.get("_rev")]) in [(change['id'], [item['rev'] for item in change['changes']]) for change in changes], #task.get("_rev") not in [task.rev for task in revisions],
                                       sort_old_project=("parentId" in updated_fields),
                                       sort_dependencies=("done" in updated_fields))
    return {"success": True, "message": f"No relevant updates, LAST_SEQ = {LAST_SEQ}"}


@app.route('/add', methods=['POST'])
@auth.login_required
def add():
    """Re-sort after a task is added, if necessary"""
    edited_task=request.get_json(silent=True)
    # if current project is not inbox (i.e. "unassigned"), sort current project
    if edited_task.get("parentId", "unassigned") != "unassigned":
        return sort_and_update_by_task(edited_task,
                                       # If we're waiting for an add, then the doc need only exist
                                       lambda task, changes: task["_id"] in LOCAL_DB or task["_id"] in [change["id"] for change in changes]) #lambda task, revisions: len(revisions) == 0)
    return {"success": True, "message": f"No relevant updates, LAST_SEQ = {LAST_SEQ}"}

@app.route('/markDone', methods=['POST'])
@auth.login_required
def markDone():
    """Re-sort after a task is marked done, if necessary"""
    edited_task=request.get_json(silent=True)
    # if done changed, need to sort current project and all tasks which depend on task
    return sort_and_update_by_task(edited_task,
                                   # If we're waiting for the update, then the new revision should either already be in the LOCAL_DB, or the id and rev should be in the change list
                                   lambda task, changes: task.get("_rev") == LOCAL_DB.get(task["_id"], {}).get("_rev") or (task["_id"], [task.get("_rev")]) in [(change['id'], [item['rev'] for item in change['changes']]) for change in changes], #task.get("_rev") not in [task.rev for task in revisions],
                                   sort_dependencies=True)


@app.route('/delete', methods=['POST'])
@auth.login_required
def delete():
    """Re-sort after a task is deleted, if necessary"""
    edited_task=request.get_json(silent=True)
    # if task deleted, need to sort current project and all tasks which depend on task
    return sort_and_update_by_task(edited_task,
                                   # If we're waiting for a deletion, either the task should no longer exist, or deleted: True should be in one of the changes
                                   lambda task, changes: task["_id"] not in LOCAL_DB or task["_id"] in [change['id'] for change in changes if change.get("deleted", False)], #lambda task, revisions: len(revisions) > 0,
                                   sort_dependencies=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
