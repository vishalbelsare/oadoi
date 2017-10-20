import os
from sickle import Sickle
from sickle.response import OAIResponse
import boto
import datetime
import requests
from time import sleep
from time import time
import re
import argparse
import shortuuid
from sqlalchemy.dialects.postgresql import JSONB

from app import db
from app import logger
from oa_pmh import PmhRecord
from util import safe_commit
from util import elapsed
from util import is_doi_url
from util import clean_doi
from util import NoDoiException


class MissingTagException(Exception):
    pass


def oai_tag_match(tagname, record, return_list=False):
    if not tagname in record.metadata:
        return None
    matches = record.metadata[tagname]
    if return_list:
        return matches  # will be empty list if we found naught
    else:
        try:
            return matches[0]
        except IndexError:  # no matches.
            return None


def is_complete(record):
    if not record.id:
        return False
    if not record.title:
        return False
    if not record.urls:
        return False

    if record.oa == "0":
        logger.info(u"record {} is closed access. skipping.".format(record["id"]))
        return False

    return True



def safe_get_next_record(records):
    try:
        next_record = records.next()
    except (requests.exceptions.HTTPError, requests.exceptions.SSLError):
        logger.info(u"requests exception!  skipping")
        return safe_get_next_record(records)
    except (KeyboardInterrupt, SystemExit):
        # done
        return None
    except StopIteration:
        # logger.info(u"stop iteration! stopping")
        return None
    except Exception:
        logger.exception(u"misc exception!  skipping")
        return safe_get_next_record(records)
    return next_record


class PmhSource(db.Model):
    id = db.Column(db.Text, primary_key=True)
    url = db.Column(db.Text)
    last_harvest_started = db.Column(db.DateTime)
    last_harvest_finished = db.Column(db.DateTime)
    last_harvested_date = db.Column(db.DateTime)

    def __init__(self, **kwargs):
        super(self.__class__, self).__init__(**kwargs)





class MySickle(Sickle):
    RETRY_SECONDS = 3
    def harvest(self, **kwargs):  # pragma: no cover
        """Make HTTP requests to the OAI server.
        :param kwargs: OAI HTTP parameters.
        :rtype: :class:`sickle.OAIResponse`
        """
        for _ in range(self.max_retries):
            if self.http_method == 'GET':
                http_response = requests.get(self.endpoint, params=kwargs,
                                             **self.request_args)
            else:
                http_response = requests.post(self.endpoint, data=kwargs,
                                              **self.request_args)
            if http_response.status_code == 503:
                retry_after = self.RETRY_SECONDS
                logger.info(
                    "HTTP 503! Retrying after %d seconds..." % retry_after)
                sleep(retry_after)
            else:
                http_response.raise_for_status()
                if self.encoding:
                    http_response.encoding = self.encoding
                return OAIResponse(http_response, params=kwargs)


def pmh_to_db(first=None,
              last=None,
              today=None,
              chunk_size=100,
              url=None):


    args = {}
    if not url:
        url="http://oai.base-search.net/oai"
        args['metadataPrefix'] = 'base_dc'
    else:
        args['metadataPrefix'] = 'oai_dc'

    if "base-search" in url or "citeseerx" in url:
        proxy_url = os.getenv("STATIC_IP_PROXY")
        proxies = {"https": proxy_url, "http": proxy_url}
    else:
        proxies = {}

    my_sickle = MySickle(url, proxies=proxies, timeout=120)
    logger.info(u"connected to sickle with {} {}".format(url, proxies))

    if today:
        last = datetime.date.today().isoformat()
        first = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()

    args['from'] = first
    if last:
        args["until"] = last

    records_to_save = []

    logger.info(u"calling ListRecords with {} {}".format(url, args))
    try:
        pmh_records = my_sickle.ListRecords(ignore_deleted=True, **args)
        logger.info(u"got pmh_records with {} {}".format(url, args))
        pmh_input_record = safe_get_next_record(pmh_records)
    except Exception as e:
        logger.info(u"no records with {} {}".format(url, args))
        # logger.exception(u"no records with {} {}".format(url, args))
        pmh_input_record = None

    while pmh_input_record:
        pmh_record = PmhRecord()

        pmh_record.id = pmh_input_record.header.identifier
        pmh_record.api_raw = pmh_input_record.raw
        pmh_record.record_timestamp = pmh_input_record.header.datestamp
        pmh_record.title = oai_tag_match("title", pmh_input_record)
        pmh_record.authors = oai_tag_match("creator", pmh_input_record, return_list=True)
        pmh_record.oa = oai_tag_match("oa", pmh_input_record)
        pmh_record.urls = oai_tag_match("identifier", pmh_input_record, return_list=True)
        for fulltext_url in pmh_record.urls:
            if fulltext_url and (is_doi_url(fulltext_url) or fulltext_url.startswith(u"doi:")):
                try:
                    pmh_record.doi = clean_doi(fulltext_url)
                except NoDoiException:
                    pass

        pmh_record.license = oai_tag_match("rights", pmh_input_record)
        pmh_record.relations = oai_tag_match("relation", pmh_input_record, return_list=True)
        pmh_record.sources = oai_tag_match("collname", pmh_input_record, return_list=True)
        pmh_record.source = url

        # print pmh_record

        if is_complete(pmh_record):
            db.session.merge(pmh_record)
            records_to_save.append(pmh_record)
            # logger.info(u":")
        # else:
        #     print "not complete"

        if len(records_to_save) >= chunk_size:
            last_record = records_to_save[-1]
            logger.info(u"last record saved: {} for {}".format(last_record.id, url))
            safe_commit(db)
            records_to_save = []

        pmh_input_record = safe_get_next_record(pmh_records)

    # make sure to get the last ones
    if records_to_save:
        last_record = records_to_save[-1]
        logger.info(u"saving {} last ones, last record saved: {} for {}".format(len(records_to_save), last_record.id, url))
        safe_commit(db)
    logger.info(u"done everything for {}".format(url))



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run stuff.")

    function = pmh_to_db
    parser.add_argument('--first', type=str, help="first date to pull stuff from oai-pmh (example: --start_date 2016-11-10")
    parser.add_argument('--last', type=str, help="last date to pull stuff from oai-pmh (example: --end_date 2016-11-10")

    parser.add_argument('--today', action="store_true", default=False, help="use if you want to pull in base records from last 2 days")

    parser.add_argument('--chunk_size', nargs="?", type=int, default=100, help="how many rows before a db commit")

    # parser.add_argument('--url', nargs="?", type=str, default="http://export.arxiv.org/oai2", help="oai-pmh url")
    #  https://www.ncbi.nlm.nih.gov/pmc/oai/oai.cgi
    parser.add_argument('--url', nargs="?", type=str, default="http://citeseerx.ist.psu.edu/oai2", help="oai-pmh url")

    parsed = parser.parse_args()

    logger.info(u"calling {} with these args: {}".format(function.__name__, vars(parsed)))
    function(**vars(parsed))
