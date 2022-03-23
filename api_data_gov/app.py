import json
import logging
import os
import pprint
import pytz
import sys
from datetime import datetime
from itertools import chain
from unittest import TestCase

import ratelimit
import pymongo
import requests

from backoff import on_exception, expo
from nested_diff import diff
from pandas import DataFrame as df

logging.basicConfig(stream=sys.stdout,
                    filemode="w",
                    format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S",
                    level=logging.INFO)

logger = logging.getLogger('api-data-gov')

HOUR_IN_SECONDS = 3600

API_KEY = os.environ["API_KEY"]
DOCKET_ID = os.environ["DOCKET_ID"]
client = pymongo.MongoClient(os.environ["MONGO_URI"])
db = client.get_database()

session = requests.Session()


class RateLimitException(requests.exceptions.RequestException):

    pass


@on_exception(expo, (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    ratelimit.RateLimitException,
    RateLimitException
), max_time=HOUR_IN_SECONDS)
@ratelimit.limits(calls=1000, period=HOUR_IN_SECONDS)
def get(url):
    response = session.get(url, headers={ "X-Api-Key": API_KEY })
    logger.info(f"Response Code: {response.status_code}")
    logger.info(pprint.pformat(response.headers, indent=2))
    if response.status_code == 429:
        raise RateLimitException(response.reason)
    if response.status_code != 200:
        logger.error(f"HTTP error: {response.reason}")
    logger.debug(pprint.pformat(json.loads(response.text), indent=2))
    return json.loads(response.text)


def to_query_string(params, prefix=''):
    return "&".join(chain(
        f"{prefix}{key if prefix == '' else f'[{key}]'}={value}"
        if not isinstance(value, dict) else
        to_query_string(value, key if prefix == '' else f"{prefix}[{key}]")
        for key, value in params.items()
    ))


def get_comment(url):
    scanned_date = datetime.now()
    comment = get(url)["data"]
    db_comment = db.comments.find_one({ "id": comment["id"]})
    orig_history = []
    if db_comment is not None:
        comment["_id"] = db_comment["_id"]

        orig_history = db_comment["_history"]
        orig_scanned_date = db_comment["_scanned"]
        del db_comment["_history"]
        del db_comment["_scanned"]

        try:
            TestCase().assertDictEqual(db_comment, comment)
        except AssertionError:
            orig_history.insert(0, diff({ **db_comment, "_scanned": orig_scanned_date }, {
                key: value for key, value in comment
            }))
    comment["_history"] = orig_history
    comment["_scanned"] = scanned_date
    return db.comments.find_one_and_replace(
        { "id": comment["id"] },
        comment,
        upsert=True,
        return_document=pymongo.ReturnDocument.AFTER
    )


def get_comments():
    params = {
        "page": { "number": 1, "size": 25 },
        "sort": "lastModifiedDate",
        "filter": {
            "docketId": DOCKET_ID
        }
    }

    while True:
        response = get(f"https://api.regulations.gov/v4/comments?{to_query_string(params)}")
        if not response["data"]:
            return
        for comment in response["data"]:
            get_comment(f"https://api.regulations.gov/v4/comments/{comment['id']}?include=attachments")

        if response["meta"]["lastPage"] == True:
            params["page"]["number"] = 1
            params["filter"]["lastModifiedDate"] = {
                "ge": datetime.strptime(comment["attributes"]["lastModifiedDate"], '%Y-%m-%dT%H:%M:%S%z')
                        .astimezone(pytz.timezone("America/New_York"))
                        .strftime("%Y-%m-%d %H:%M:%S")
            }
            continue
        params["page"]["number"] = int(response["meta"]["pageNumber"]) + 1

def publish():
    df.from_dict(
        {
            "id": comment["id"],
            "link": comment["links"]["self"],
            **comment["attributes"],
            "_scanned": comment["_scanned"],
            "_history": comment["_history"]
        }
        for comment in db.comments.find()
    ).to_html("out/index.html")
