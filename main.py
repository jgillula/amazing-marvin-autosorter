#!/usr/bin/env python3

import os
import json
import time

# We use flask to handle the HTTP requests
# https://flask.palletsprojects.com/en/2.0.x/
from flask import Flask, request, send_from_directory

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
access_tokens = json.loads(os.environ.get("ACCESS_TOKENS_LIST", '[]'))
couchdb_username = os.environ.get("COUCHDB_USERNAME", "")
couchdb_password = os.environ.get("COUCHDB_PASSWORD", "")
couchdb_hostname = os.environ.get("COUCHDB_HOSTNAME", "")
couchdb_database = os.environ.get("COUCHDB_DATABASE", "")
sort_list = json.loads(os.environ.get("SORT_LIST", '[]'))
UPDATE_TIMEOUT_MILLISECONDS = os.environ.get("UPDATE_TIMEOUT_MILLISECONDS", 5000)
time_between_queries = 1/os.environ.get("QPS_RATE_LIMIT", 1)


# This function lets us globally rate limit calls to the Amazing Marvin backend database
last_query_time = 0
def rate_limit(func):
    global last_query_time
    time.sleep(max(0, last_query_time + time_between_queries - time.time()))
    last_query_time = time.time()
    return func()


# Setup the database, and initialize the local copy of the database
if couchdb_username and couchdb_password and couchdb_hostname and couchdb_database:
    server = couchdb.Server(f"https://{couchdb_username}:{couchdb_password}@{couchdb_hostname}")
    db = server[couchdb_database]    
    last_seq = rate_limit(lambda: db.info().get('update_seq'))
    local_db = dict([(item.id, item) for item in rate_limit(lambda: db.find({"selector": {"$or": [{"db": "Tasks"}, {"db": "Categories"}]}}))])
else:
    db = None
    last_seq = ""
    local_db = dict()


# Update our local_db given a list of changes that returned from db.changes(...)
def update_db(changes):
    global local_db
    local_db.update([(item.id, item) for item in rate_limit(lambda: db.find({"selector": {"$or": [{"_id": id} for id in set([change['id'] for change in changes if not change.get('deleted', False)])]}})) if item['db']=="Tasks" or item["db"]=="Categories"])
    for id_to_delete in set([change['id'] for change in changes if change.get("deleted", False) and change['id'] in local_db]):
        del local_db[id_to_delete]

    
def label_key(task, options):
    for label_title in options["labels"]:
        if label_titles_to_ids[label_title] in task.get('labelIds', []):
            return options["labels"].index(label_title)
    if options.get("no_match_last", True):
        return len(options["labels"])
    else:
        return -1


label_titles_to_ids = {}
def label_key_factory(options):
    global label_titles_to_ids
    if not label_titles_to_ids:
        result = rate_limit(lambda: db.get("strategySettings.labels"))
        if result:
            label_titles_to_ids = dict([(label['title'], label['_id']) for label in result.get('val', [])])
    
    return (lambda task: label_key(task, options))


def is_ready(task):
    ready = (task.get('backburner', False) == False)
    dependsOn = task.get('dependsOn', {})
    if isinstance(dependsOn, dict):
        ready = (ready and all([local_db.get(key, {"done": True}).get('done', False) for key in dependsOn.keys()]))
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


# edited_task is the one that changed
# db_updated is a function that takes the edited_task and the changes and returns True if the edit is reflected in the changes or local_db
def sort_and_update_by_task(edited_task, db_updated, sort_old_project=False, sort_dependencies=False):    
    # First we check that the edited_task has all the fields we're going to need
    if not isinstance(edited_task, dict) or not all([field in edited_task.keys() for field in ["_id", "parentId"]]):
        return {"success": False, "message": "edited_task wasn't a dict or didn't have the required fields"}

    # start by including the current project in the list of tasks to sort
    parent_ids = set([edited_task["parentId"]])

    if sort_old_project:
        # This gets the parentId of the task from the list of previous revisions (assuming the parentId wasn't "unassigned", which means inbox)
        old_parent_id = local_db[edited_task["_id"]]['parentId']
        if old_parent_id != "unassigned":
            parent_ids.update(old_parent_id)

    # Next we wait until the database is updated
    changes = []
    global last_seq
    if not db_updated(edited_task, changes):
        change_feed = rate_limit(lambda: db.changes(since=last_seq, feed="continuous", timeout=UPDATE_TIMEOUT_MILLISECONDS))
        for change in change_feed:
            if 'last_seq' in change:
                last_seq = change['last_seq']
            else:
                changes.append(change)
                if db_updated(edited_task, changes):
                    last_seq = change['seq']
                    break
        global last_query_time
        last_query_time = time.time()
        # Download the changes and update our db
        update_db(changes)
    # If we're still waiting for the database to be updated, return failure
    if not db_updated(edited_task, changes):
        return {"success": False, "message": "timed out waiting for deletion, last_seq = {}".format(last_seq)}

    if sort_dependencies:
        # This crazy one-liner uses a list comprehension to get all parentIds of tasks for which the edited_task's id is in the task's dependsOn list
        parent_ids.update([task['parentId'] for task in local_db.values() if edited_task["_id"] in (task.get('dependsOn', {}) if task.get('dependsOn') is not None else {}).keys()])

    return sort_and_update_by_parent_ids(parent_ids)


def sort_and_update_by_parent_ids(parent_ids):
    # This gets all the tasks in all the projects in parent_ids
    affected_tasks = list([item for item in local_db.values() if item['parentId'] in parent_ids])
    # We sort per-project, but will update all the changes at once
    updated_docs = []
    for parent_id in parent_ids:
        project_tasks = [task for task in affected_tasks if task['parentId'] == parent_id]
        # do the sorting
        for key, reverse in processed_sort_list:
            project_tasks.sort(key=key, reverse=reverse)
        # cycle through  all of the tasks by index
        for i in range(len(project_tasks)):
            # if its old masterRank doesn't equal it's new index+1 (+1 because masterRank starts at 1, not 0)
            if project_tasks[i].get('masterRank') != i+1:
                project_tasks[i]['masterRank'] = i+1
                updated_docs.append(project_tasks[i])
    # Update the changed docs on the server
    result = rate_limit(lambda: db.update(updated_docs))
    if not all(item[1] for item in result):
        return {"success": False, "message": "Error updating docs in database"}
    return {"success": True, "message": "Updated {}/{} docs, last_seq = {}".format(len(updated_docs), len(affected_tasks), last_seq)}    


@auth.verify_token
def verify_token(token):
    app.logger.debug("Trying to verify with token {}".format(token))
    return token in access_tokens


@app.before_request
def check_db_connection():
    if not db:
        return {"success": False, "message": "db connection failed"}


@app.route('/<string:access_token>/sortAll')
def sortAll(access_token):
    global last_seq
    if verify_token(access_token):
        changes = rate_limit(lambda: db.changes(since=last_seq))
        update_db(changes["results"])
        last_seq = changes["last_seq"]
        parent_ids = [item.id for item in local_db.values() if item['db'] == "Categories"] #[project["_id"] for project in db.find({"selector": {"db": "Categories"}})]
        result =  sort_and_update_by_parent_ids(parent_ids)
        if result["success"]:
            return(send_from_directory(".", "close.html"))
        else:
            return result
    else:
        abort(404)    

    
@app.route('/edit', methods=['POST'])
@auth.login_required
def edit():
    global last_seq
    edited_task = request.get_json(silent=True)
    updated_fields = edited_task.get("setter", {}).keys()
    if any([updated_field in relevant_fields for updated_field in updated_fields]):
        # if done changed, need to sort current project and all tasks which depend on task
        # if parentId changed, need to sort old project and current project
        return sort_and_update_by_task(edited_task,
                                       # If we're waiting for the update, then the new revision should either already be in the local_db, or the id and rev should be in the change list
                                       lambda task, changes: task.get("_rev") == local_db.get(task["_id"], {}).get("_rev") or (task["_id"], [task.get("_rev")]) in [(change['id'], [item['rev'] for item in change['changes']]) for change in changes], #task.get("_rev") not in [task.rev for task in revisions],
                                       sort_old_project=("parentId" in updated_fields),
                                       sort_dependencies=("done" in updated_fields))
    return {"success": True, "message": "No relevant updates, last_seq = {}".format(last_seq)}


@app.route('/add', methods=['POST'])
@auth.login_required
def add():
    edited_task=request.get_json(silent=True)
    # if current project is not inbox (i.e. "unassigned"), sort current project    
    if edited_task.get("parentId", "unassigned") != "unassigned":
        return sort_and_update_by_task(edited_task,
                                       # If we're waiting for an add, then the doc need only exist
                                       lambda task, changes: task["_id"] in local_db or task["_id"] in [change["id"] for change in changes]) #lambda task, revisions: len(revisions) == 0)
            

@app.route('/markDone', methods=['POST'])
@auth.login_required
def markDone():
    edited_task=request.get_json(silent=True)
    # if done changed, need to sort current project and all tasks which depend on task    
    return sort_and_update_by_task(edited_task,
                                   # If we're waiting for the update, then the new revision should either already be in the local_db, or the id and rev should be in the change list
                                   lambda task, changes: task.get("_rev") == local_db.get(task["_id"], {}).get("_rev") or (task["_id"], [task.get("_rev")]) in [(change['id'], [item['rev'] for item in change['changes']]) for change in changes], #task.get("_rev") not in [task.rev for task in revisions],
                                   sort_dependencies=True)


@app.route('/delete', methods=['POST'])
@auth.login_required
def delete():
    edited_task=request.get_json(silent=True)
    # if task deleted, need to sort current project and all tasks which depend on task
    return sort_and_update_by_task(edited_task,
                                   # If we're waiting for a deletion, either the task should no longer exist, or deleted: True should be in one of the changes
                                   lambda task, changes: task["_id"] not in local_db or task["_id"] in [change['id'] for change in changes if change.get("deleted", False)], #lambda task, revisions: len(revisions) > 0,
                                   sort_dependencies=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
