from turtle import st
from beanie import init_beanie
import motor

from bson.binary import Binary

from datetime import datetime
from uuid import UUID
from typing import List
from ibex_models import  Post, Annotations, TextForAnnotation, Monitor, Platform, Account, SearchTerm, CollectAction, CollectTask
from model import PostRequestParams, RequestAnnotations, PostRequestParamsAggregated, PostMonitor, TagRequestParams, IdRequestParams, SearchAccountsRequest
from beanie.odm.operators.find.comparison import In

from bson import json_util, ObjectId
from bson.json_util import dumps, loads
from pydantic import Field, BaseModel, validator

import json 

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
import os
import subprocess

from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuthError
from jwt_ import create_refresh_token
from jwt_ import create_token
from jwt_ import CREDENTIALS_EXCEPTION
from jwt_ import get_current_user_email

from authlib.integrations.starlette_client import OAuth
from starlette.config import Config

# OAuth settings
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID') or None
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET') or None

if GOOGLE_CLIENT_ID is None or GOOGLE_CLIENT_SECRET is None:
    raise BaseException('Missing env variables')

# Set up OAuth
config_data = {'GOOGLE_CLIENT_ID': GOOGLE_CLIENT_ID, 'GOOGLE_CLIENT_SECRET': GOOGLE_CLIENT_SECRET}
starlette_config = Config(environ=config_data)
oauth = OAuth(starlette_config)
oauth.register(
    name='google',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)


app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
    
app.add_middleware(SessionMiddleware, secret_key='SECRET_KEY')

# @staticmethod
def generate_search_criteria(post_request_params: PostRequestParams):
    search_criteria = {
        'created_at': { '$gte': post_request_params.time_interval_from, '$lte': post_request_params.time_interval_to},
    }
    
    if bool(post_request_params.monitor_id):
        search_criteria['monitor_ids'] = { '$in': [UUID(post_request_params.monitor_id)] }  

    if type(post_request_params.has_video) == bool:
        search_criteria['has_video'] = { '$eq': post_request_params.has_video }

    if bool(post_request_params.post_contains):
        search_criteria['text'] = { '$regex': post_request_params.post_contains }

    if len(post_request_params.platform) > 0:
        search_criteria['platform'] = { '$in': post_request_params.platform }

    if len(post_request_params.accounts) > 0:
        search_criteria['account_id'] = { '$in': [Binary(i.bytes, 3) for i in post_request_params.accounts] }

    if len(post_request_params.author_platform_id) > 0:
        search_criteria['author_platform_id'] = { '$in': post_request_params.author_platform_id }
    
    if len(post_request_params.topics) > 0:
        search_criteria['labels.topics'] = { '$in': [UUID(i) for i in post_request_params.topics] }
    
    if len(post_request_params.persons) > 0:
        search_criteria['labels.persons'] = { '$in': [UUID(i) for i in post_request_params.persons] }

    if len(post_request_params.locations) > 0:
        search_criteria['labels.locations'] = { '$in': [UUID(i) for i in post_request_params.locations] }

    return search_criteria


async def mongo(classes):
    mongodb_connection_string = os.getenv('MONGO_CS')
    client = motor.motor_asyncio.AsyncIOMotorClient(mongodb_connection_string)
    await init_beanie(database=client.ibex, document_models=classes)


def json_responce(result):
    json_result = json.loads(json_util.dumps(result))
    return JSONResponse(content=jsonable_encoder(json_result), status_code=200)


@app.post("/posts", response_description="Get list of posts", response_model=List[Post])
async def posts(post_request_params: PostRequestParams, current_email: str = Depends(get_current_user_email)) -> List[Post]:
    await mongo([Post])
    
    search_criteria = generate_search_criteria(post_request_params)

    result = await Post.find(search_criteria)\
        .aggregate([
            {   '$sort': { 'created_at': -1 }},
            {
                '$skip': post_request_params.start_index
            },
            {
                '$limit': post_request_params.count
            },
            {
                '$lookup': {
                        'from': "tags",
                        'localField': "labels.topics",
                        'foreignField': "_id",
                        'as': "labels.topics"
                    }
            },
            {
                '$lookup': {
                        'from': "tags",
                        'localField': "labels.locations",
                        'foreignField': "_id",
                        'as': "labels.locations"
                    }
            },
            {
                '$lookup': {
                        'from': "tags",
                        'localField': "labels.persons",
                        'foreignField': "_id",
                        'as': "labels.persons"
                    }
            },
            {
                '$lookup': {
                    'from': "accounts",
                    'localField': f"account_id",
                    'foreignField': "_id",
                    'as': "account"
                }
            },
        ])\
        .to_list()

    for result_ in result:
        result_['api_dump'] = ''
        # for result_key in result_.keys():
        #     if type(result_[result_key]) == float:
        #         result_[result_key] = 'NN'
        
    return json_responce(result)


@app.post("/posts_aggregated", response_description="Get aggregated data for posts")#, response_model=List[Post])
async def posts_aggregated(post_request_params_aggregated: PostRequestParamsAggregated, current_email: str = Depends(get_current_user_email)):
    
    await mongo([Post])
    
    search_criteria = generate_search_criteria(post_request_params_aggregated.post_request_params)
    
    axisX = f"${post_request_params_aggregated.axisX}" \
        if post_request_params_aggregated.axisX in ['platform', 'author_platform_id'] \
        else f"$labels.{post_request_params_aggregated.axisX}" 

    aggregation = {}
    if post_request_params_aggregated.days is None:
        aggregation = axisX
    else:
        aggregation = {
            "label": axisX,
            "year": { "$year": "$created_at" },
        }
        if post_request_params_aggregated.days == 30:
            aggregation["month"] = { "$month": "$created_at" }
        if post_request_params_aggregated.days == 7:
            aggregation["week"] = { "$week": "$created_at" }
        if post_request_params_aggregated.days == 1:
            aggregation["day"] = { "$dayOfYear": "$created_at" }
    
    aggregations = []    
    if post_request_params_aggregated.axisX not in ['platform', 'author_platform_id']:
        aggregations.append({'$unwind':axisX })

    group = {'$group': {
        '_id': aggregation , 
        'count': {'$sum':1}
        } 
    }

    aggregations.append({'$group': {
        '_id': aggregation , 
        'count': {'$sum':1}
        } 
    })
    
    if post_request_params_aggregated.axisX not in ['platform', 'author_platform_id']:
        aggregations.append({
                '$lookup': {
                    'from': "tags",
                    'localField': f"_id{'.label' if post_request_params_aggregated.days is not None else ''}",
                    'foreignField': "_id",
                    'as': f"{post_request_params_aggregated.axisX}"
                }
            })
        aggregations.append({'$unwind': f"${post_request_params_aggregated.axisX}" })
    else:
        set_ = { '$set': {} }
        set_['$set'][post_request_params_aggregated.axisX] = '$_id'
        aggregations.append(set_)

    # print(aggregations)
    # print(search_criteria)
    result = await Post.find(search_criteria)\
        .aggregate([
            *aggregations,
        ])\
        .to_list()

    return json_responce(result)


@app.post("/post", response_description="Get post details")
async def post(postRequestParamsSinge: IdRequestParams, current_email: str = Depends(get_current_user_email)) -> Post:
    await mongo([Post])
    id_ = loads(f'{{"$oid":"{postRequestParamsSinge.id}"}}')

    post = await Post.find_one(Post.id == id_)
    post.api_dump = {}
    return JSONResponse(content=jsonable_encoder(post), status_code=200)


@app.post("/create_monitor", response_description="Create monitor")
async def create_monitor(postMonitor: PostMonitor, current_email: str = Depends(get_current_user_email)) -> Monitor:
    await mongo([Monitor, Account, SearchTerm, CollectAction])

    monitor = Monitor(
        title=postMonitor.title, 
        descr=postMonitor.descr, 
        collect_actions = [], 
        date_from=postMonitor.date_from, 
        date_to=postMonitor.date_to
    )
    # print(monitor.id, type(monitor.id), type(str(monitor.id)))
    search_terms = [SearchTerm(
            term=search_term, 
            tags=[str(monitor.id)]
        ) for search_term in postMonitor.search_terms]

    accounts = [Account(
            title = account.title, 
            platform = account.platform, 
            platform_id = account.platform_id, 
            tags = [str(monitor.id)],
            url=''
        ) for account in postMonitor.accounts]
    
    platforms = postMonitor.platforms if postMonitor.platforms and len(postMonitor.platforms) else [account.platform for account in postMonitor.accounts]
    
    # Create single CollectAction per platform
    collect_actions = [CollectAction(
            monitor_id = monitor.id,
            platform = platform, 
            search_term_tags = [str(monitor.id)], 
            account_tags=[str(monitor.id)],
            tags = [],
        ) for platform in platforms]
    
    monitor.collect_actions = [collect_action.id for collect_action in collect_actions]

    if len(search_terms): await SearchTerm.insert_many(search_terms)
    if len(accounts): await Account.insert_many(accounts)
    await CollectAction.insert_many(collect_actions)
    await monitor.save()

    return monitor


@app.post("/update_monitor", response_description="Create monitor")
async def update_monitor(monitor_: Monitor, current_email: str = Depends(get_current_user_email)) -> Monitor:
    monitor = await Monitor.get(monitor_.id)
    for key, value in monitor_.__dict__:
        monitor[key] = value
    await monitor.save()


@app.post("/collect_sample", response_description="Run sample collection pipeline")
async def collect_sample(monitor_id: IdRequestParams, current_email: str = Depends(get_current_user_email)):
    cmd = f'python3 /root/data-collection-and-processing/main.py --monitor_id={monitor_id.id} --sample=True >> api.out'
    subprocess.Popen(cmd, stdout=None, stderr=None, stdin=None, close_fds=True, shell=True)
    # os.popen(cmd).read()


@app.post("/get_hits_count", response_description="Get amount of post for monitor")
async def get_hits_count(postRequestParamsSinge: IdRequestParams, current_email: str = Depends(get_current_user_email)):
    await mongo([CollectTask])

    collect_tasks = await CollectTask.find(CollectTask.monitor_id == UUID(postRequestParamsSinge.id)).to_list()
    # counts = {} 
    # for platform in Platform:
    #     counts[platform] = sum([collect_task.hits_count or 0 for collect_task in collect_tasks if collect_task.platform == platform])
    from itertools import groupby
    getTerm = lambda collect_task: collect_task.search_terms[0].term


    terms_with_counts = {}
    for _name, _list in groupby(sorted(collect_tasks, key=getTerm), key=getTerm):
        terms_with_counts[_name] = {}
        for item in _list:
            terms_with_counts[_name][item.platform] = item.hits_count
    return JSONResponse(content=jsonable_encoder(terms_with_counts), status_code=200)

    
@app.post("/get_monitor", response_description="Get monitor")
async def search_account(monitor_id: IdRequestParams, current_email: str = Depends(get_current_user_email)):
    await mongo([Monitor, SearchTerm, Account])
    monitor = await Monitor.get(monitor_id.id)
    search_terms = await SearchTerm.find(In(SearchTerm.tags, [monitor_id.id])).to_list()
    accounts = await Account.find(In(SearchTerm.tags, [monitor_id.id])).to_list()
    return { 'monitor': monitor, 'search_terms': search_terms, 'accounts': accounts }


@app.post("/get_monitors", response_description="Get monitors")
async def get_monitors(post_tag: TagRequestParams, current_email: str = Depends(get_current_user_email)) -> Monitor:
    await mongo([Monitor])
    if post_tag.tag == '*':
        monitor = await Monitor.find().to_list()
    else:
        monitor = await Monitor.find(In(Monitor.tags, post_tag.tag)).to_list()

    return JSONResponse(content=jsonable_encoder(monitor), status_code=200)


@app.post("/search_account", response_description="Search accounts by string across all platforms")
async def search_account(search_accounts: SearchAccountsRequest, current_email: str = Depends(get_current_user_email)):
    # post_tag.tag
    pass


@app.post("/save_and_next", response_description="Save the annotations for the text and return new text for annotation", response_model=TextForAnnotation)
async def save_and_next(request_annotations: RequestAnnotations) -> TextForAnnotation:
    await mongo([Annotations, TextForAnnotation])
    
    annotations = Annotations(text_id = request_annotations.text_id, user_mail = request_annotations.user_mail, annotations = request_annotations.annotations)

    await annotations.insert()

    already_annotated = await Annotations.aggregate([
        {"$match": { "user_mail": { "$eq": 'djanezashvili@gmail.com' }}}
    ]).to_list()
    annotated_text_ids = [annotations["text_id"]  for annotations in already_annotated]

    text_for_annotation = await TextForAnnotation.aggregate([
        {"$match": { "_id": { "$nin": annotated_text_ids }}},
        {
            "$lookup":
                {
                    "from": "annotations",
                    "localField": "_id",
                    "foreignField": "text_id",
                    "as": "annotations_"
                }
        }, 
        {"$unwind": "$annotations_"},
        {"$group" : {"_id":"$_id", "count":{"$sum":1}}},
        {"$match": { "count": { "$lt": 4 }}},
        {"$sample": {"size": 1}},
        {
            "$lookup":
                {
                    "from": "text_for_annotation",
                    "localField": "_id",
                    "foreignField": "_id",
                    "as": "text"
                }
        },
        {"$unwind": "$text"},
    ]).to_list()

    text_for_annotation = TextForAnnotation(id=text_for_annotation[0]["_id"], post_id = text_for_annotation[0]["text"]["post_id"], words=text_for_annotation[0]["text"]["words"])
    return text_for_annotation


@app.get('/login')
async def login(request: Request):
    env = 'dev' if 'localhost' in request.headers['referer'] else 'prod'
    redirect_uri = f'https://ibex-app.com/api/token?env={env}'
    redirect = await oauth.google.authorize_redirect(request, redirect_uri)
    return redirect


@app.route('/token')
async def auth(request: Request):
    try:
        access_token = await oauth.google.authorize_access_token(request)
    except OAuthError as err:
        print(str(err))
        raise CREDENTIALS_EXCEPTION

    user_data = await oauth.google.parse_id_token(access_token, access_token['userinfo']['nonce'])

    if user_data['email'] in ['alexisadams@gmail.com', 'edekeulenaar@gmail.com', 'djanezashvili@gmail.com', 'naroushvili.d@gmail.com', 'klachashvili@gmail.com', 'nikamamuladze97@gmail.com', 'ninako.chokheli@gmail.com', 'likakhutsiberidze@gmail.com', 'mariamtsitsikashvili@gmail.com']:
        obj_ = {
            'result': True,
            'access_token': create_token(user_data['email']).decode("utf-8") ,
            'refresh_token': create_refresh_token(user_data['email']).decode("utf-8") ,
        }
        return_url = 'http://localhost:3000' if request.query_params['env'] == 'dev' else 'https://ibex-app.com'
        return RedirectResponse(url=f"{return_url}?access_token={obj_['access_token']}&user={user_data['email']}")

    raise CREDENTIALS_EXCEPTION

# create_monitor, update_monitor, get_monitors, collect_sample, get_hits_count, search_account

# curl -X 'POST' \
#   'http://161.35.73.100:8888/create_monitor' \
#   -H 'accept: application/json' \
#   -H 'Content-Type: application/json' \
#   -d '{  "title": "Testing Monitor api", "descr": "descr", "date_from": "2022-02-01T00:00:00.000Z", "search_terms": ["ზუგდიდი"], "platforms": ["facebook"], "accounts": [{"title": "title",  "platform_id": "platform_id", "platform": "facebook"}] }'


# curl -X 'POST' \
#   'http://161.35.73.100:8888/get_monitors' \
#   -H 'accept: application/json' \
#   -H 'Content-Type: application/json' \
#   -d '{ "tag": "*"}'

# curl -X 'POST' \
#   'http://161.35.73.100:8888/collect_sample' \
#   -H 'accept: application/json' \
#   -H 'Content-Type: application/json' \
#   -d '{ "id": "f06a6a87-7fb8-4a53-8c46-90accd89aa8c"}'