[![pylint Status](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/jgillula/e5c61344f9fd473973d9cfcd67000a96/raw/test.json)](https://github.com/jgillula/amazing-marvin-autosorter/actions/workflows/pylint.yml)

# Amazing Marvin Auto-Sorter

A docker container you can run to automatically sort your [Amazing Marvin](https://amazingmarvin.com) tasks and projects every time they change.

## How it works 

Amazing Marvin offers a set of [webhooks](https://github.com/amazingmarvin/MarvinAPI/wiki/Webhooks) that can contact a server of your choosing every time a certain actions is performed. Amazing Marvin also provides direct access to the [underlying database where your tasks and projects are stored](https://github.com/amazingmarvin/MarvinAPI/wiki/Database-Access). Put those two together, and you can have Amazing Marvin post an HTTP request to your docker container when a change happens, and your docker container can use the database access to re-sort the tasks.

## Why is it necessary?

Unfortunately Amazing Marvin doesn't let you create persistent sorts based on whether or not a task is done, or whether or not it's on the backburner, or whether or not it has dependencies. This means even when you check off a task, it won't move to the bottom of your master list. It also means that if a task is blocked waiting on other tasks, there's no way to automatically move it to the bottom of your list.

## How to use it

### 1. Setup and run the docker container

You can get the docker container from [Docker Hub](https://hub.docker.com/r/flyingsaucrdude/amazing-marvin-autosorter) by doing:
```
docker pull flyingsaucrdude/amazing-marvin-autosorter
```

I use [Google Cloud Run](https://cloud.google.com/run) to host the container, but you can use any service you like as long as it hosts the container at a publicly accessible URL.

#### First time setup
To run the container on Google Cloud Run, first [create a project](https://console.cloud.google.com/projectcreate) and remember its Project ID. Then enable the [Cloud Run API](http://console.cloud.google.com/apis/library/run.googleapis.com) and the [Artifact Registry API](https://console.cloud.google.com/flows/enableapi?apiid=artifactregistry.googleapis.com&redirect=https://cloud.google.com/artifact-registry/docs/docker/quickstart) for that project. Then on your local machine, assuming you already pulled the container from Docker Hub, you need to do the following to set things up from scratch:
```
export PROJECT_ID={PROJECT_ID} # <-- Fill in your Project ID here
sudo apt install google-cloud-sdk
gcloud auth login
gcloud config set project $PROJECT_ID
gcloud auth configure-docker us-west1-docker.pkg.dev
gcloud components install docker-credential-gcr
gcloud artifacts repositories create amazing-marvin-autosorter-repo --repository-format=docker --location=us-west1
```

#### Deploying/redeploying the container
Then to deploy the container (or a new version of it):
```
docker tag flyingsaucrdude/amazing-marvin-autosorter:latest us-west1-docker.pkg.dev/$PROJECT_ID/amazing-marvin-autosorter-repo/amazing-marvin-autosorter
docker push us-west1-docker.pkg.dev/$PROJECT_ID/amazing-marvin-autosorter-repo/amazing-marvin-autosorter
gcloud run deploy amazing-marvin-autosorter --image us-west1-docker.pkg.dev/$PROJECT_ID/amazing-marvin-autosorter-repo/amazing-marvin-autosorter
```
Once it's deployed, take note of the Service URL--you'll need that later when configuring Amazing Marvin.

### 2. Configure the docker container

The docker container is configured via environment variables. If you use Google Cloud Run like me, I find it's easiest to configure them by just [deploying a new revision](https://console.cloud.google.com/run/deploy/us-west1/amazing-marvin-autosorter) (don't forget to select your project from the dropdown at the top after clicking that link) and then editing them in the "Variables & Secrets" tab.

The following environment variables are **required**:

* `COUCHDB_USERNAME` - The username for the Amazing Marvin couchdb instance, available [here](https://app.amazingmarvin.com/pre?api) as `syncUser`
* `COUCHDB_PASSWORD` - The password for the Amazing Marvin couchdb instance, available [here](https://app.amazingmarvin.com/pre?api) as `syncPassword`
* `COUCHDB_HOSTNAME` - The hostname for the Amazing Marvin couchdb instance, available [here](https://app.amazingmarvin.com/pre?api) as `syncServer`
* `COUCHDB_DATABASE` - The specific database within the Amazing Marvin couchdb instance, available [here](https://app.amazingmarvin.com/pre?api) as `syncDatabase`
* `ACCESS_TOKENS_LIST` - This is a JSON list of strings, each of which is an access token that the docker container will use to authenticate the clients that connect to it. I recommend using something long and random, like the output of 
   ```
   import random, string
   print(''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(64)))
   ```
   It's OK if there is only one item in this list and you just use the same access token everywhere, but it's a list in case you want to have different clients use different access tokens.
* `SORT_LIST` - A JSON list that descripbes the sort you want automatically applied after any changes. See [Sort list syntax](#sort-list-syntax) for the syntax.

The following environment variables are *optional*:

* `UPDATE_TIMEOUT_MILLISECONDS` - When the docker container is triggered, it needs to make sure Amazing Marvin's backend database is updated before it should start sorting. This is how long the docker container will wait for the backend database to be updated before giving up. Default is 5000 milliseconds.
* `QPS_RATE_LIMIT` - The rate limit in queries per second for queries from the docker container to the Amazing Marvin backend database. The nice folks at Amazing Marvin [ask that you not submit more than one query per second](https://github.com/amazingmarvin/MarvinAPI/wiki#rate-limits), so you should make this no greater than 1.0 (which is the default).

### 3. Configure Amazing Marvin

The next step is to configure Amazing Marvin.

1. Since Amazing Marvin's webhooks can't be setup to operate on all tasks, we'll create a Smart List which will include all tasks. To do so, create a new smart list, name it whatever you want (e.g. "All Tasks"), and under "Filters" set "Item Type" to "Tasks". Then save.
2. Go to the API feature settings, and add the following webhooks:

   **Trigger** | **Method** | **URL**
   --- | --- | ---
   Edit | `POST` | `{DOCKER_URL}/edit`
   Add Task/Project | `POST` | `{DOCKER_URL}/add`
   Mark Done | `POST` | `{DOCKER_URL}/markDone`
   Delete | `POST` | `{DOCKER_URL}/delete`
   
   where `{DOCKER_URL}` is the URL of your docker container (e.g. `https://some.domain.com`). Set the Smart List for each webhook to the "All Tasks" list you created in step one. Finally, for **each** webhook set the headers to:
   ```
   Authorization: Bearer {ACCESS_TOKEN}
   ContentType: application/json
   ```
   where `{ACCESS_TOKEN}` is one of the access tokens you created back when you were configuring the docker container.
3. Finally, if you change something and it doesn't trigger a sort, you can always trigger one manually by visiting `{DOCKER_URL}/{ACCESS_TOKEN}/sortAll` (where again, `{DOCKER_URL}` is the URL of your docker container and `{ACCESS_TOKEN}` is one of the access tokens you created). If the sort is successful, the page will automatically close itself as soon as the sort is done. As a neat trick, you can add this as an external link in your customizable sidebar (or bottom bar, or anywhere else Amazing Marvin lets you put an external link). That way you can trigger a manual sort directly from within Marvin.

## Sort list syntax

`SORT_LIST` should be a JSON list, where each item in the list is another JSON list where the first item is a string specifying the type of sort, and the second item is a dictionary specifying the options for that sort.

The types of sorts and their options are:
- `"field"` -- Sorts by a built-in Amazing Marvin field.
  - `"field_name"` -- The name of the field as a string. Valid fields are [listed here](https://github.com/amazingmarvin/MarvinAPI/wiki/Marvin-Data-Types#tasks).
  - `"empty_value"` -- The value to use when sorting if the field is not present in a task.
  - `"reverse"` -- *Optional* Set to either `true` or `false` to control the order of the sort. Defaults to `false`.
- `"is_ready"` -- Sorts by whether tasks are ready or not (i.e. not on the backburner, and not blocked by any dependencies that aren't done). By default, tasks that aren't ready appear before tasks that are (since `False==0` and `True==1` and `0 < 1`).
  - `"reverse"` -- *Optional* Set to either `true` or `false` to control the order of the sort. Defaults to `false`.
- `"label"`
  - `"labels"` -- A list of label names as strings in the order you want to sort them.
  - `"no_match_last"` -- *Optional* Set to `true` if you want tasks without the label to be sorted at the end, or `false` if you want them at the beginning. Defaults to `true`.
  - `"reverse"` -- *Optional* Set to either `true` or `false` to control the order of the sort. Defaults to `false`.

So for example, if we set `SORT_LIST` to
```
[
    ["field", {"field_name": "done", 
               "empty_value": false}],
    ["is_ready", {"reverse": true}],
    ["label", {"labels": ["In Progress", "Next", "Later", "Waiting"]}],
    ["field", {"field_name": "isStarred", 
               "empty_value": 0, 
               "reverse": true}]
]
```
Then the Amazing Marvin Autosorter would:
1. Sort by the `done` field, treating tasks that don't have the `done` field as `False`. Note that `False` comes before `True`, so tasks where `done==False` will be above tasks that are done.
2. Within the previous sort, sort tasks that are ready above tasks that are not (since `reverse` was `True`).
3. Within the previous sort, sort tasks by the given labels, in that order, with tasks that don't have any of those labels at the end.
4. Within the previous sort, sort tasks so that high priority tasks are at the top and low priority tasks are at the bottom (and tasks with no priority are at the end), since Amazing Marvin sets isStarred to a higher value the higher the priority and we reversed the sort.
