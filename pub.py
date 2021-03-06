from time import time
import datetime
from lxml import etree
from threading import Thread
import requests
import shortuuid
import re
import random
import json
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import orm
from collections import Counter
from collections import defaultdict

from app import db
from app import logger
from util import clean_doi
from util import safe_commit
from util import NoDoiException
from util import normalize
from util import normalize_title
from util import elapsed
from util import delete_key_from_dict
import oa_local
from oa_pmc import query_pmc
from pmh_record import PmhRecord
from pmh_record import title_is_too_common
from pmh_record import title_is_too_short
import oa_manual
from open_location import OpenLocation
from reported_noncompliant_copies import reported_noncompliant_url_fragments
from webpage import PublisherWebpage
# from abstract import Abstract
from http_cache import get_session_id
from page import PageDoiMatch
from page import PageTitleMatch



def build_new_pub(doi, crossref_api):
    my_pub = Pub(id=doi, crossref_api_raw_new=crossref_api)
    my_pub.title = my_pub.crossref_title
    my_pub.normalized_title = normalize_title(my_pub.title)
    return my_pub

def add_new_pubs(pubs_to_commit):
    if not pubs_to_commit:
        return []

    pubs_indexed_by_id = dict((my_pub.id, my_pub) for my_pub in pubs_to_commit)
    ids_already_in_db = [id_tuple[0] for id_tuple in db.session.query(Pub.id).filter(Pub.id.in_(pubs_indexed_by_id.keys())).all()]
    pubs_to_add_to_db = []

    for (pub_id, my_pub) in pubs_indexed_by_id.iteritems():
        if pub_id in ids_already_in_db:
            # merge if we need to
            pass
        else:
            pubs_to_add_to_db.append(my_pub)
            # logger.info(u"adding new pub {}".format(my_pub.id))

    if pubs_to_add_to_db:
        logger.info(u"adding {} pubs".format(len(pubs_to_add_to_db)))
        db.session.add_all(pubs_to_add_to_db)
        safe_commit(db)
    return pubs_to_add_to_db


def call_targets_in_parallel(targets):
    if not targets:
        return

    # logger.info(u"calling", targets)
    threads = []
    for target in targets:
        process = Thread(target=target, args=[])
        process.start()
        threads.append(process)
    for process in threads:
        try:
            process.join(timeout=60*10)
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            logger.exception(u"thread Exception {} in call_targets_in_parallel. continuing.".format(e))
    # logger.info(u"finished the calls to {}".format(targets))

def call_args_in_parallel(target, args_list):
    # logger.info(u"calling", targets)
    threads = []
    for args in args_list:
        process = Thread(target=target, args=args)
        process.start()
        threads.append(process)
    for process in threads:
        try:
            process.join(timeout=60*10)
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            logger.exception(u"thread Exception {} in call_args_in_parallel. continuing.".format(e))
    # logger.info(u"finished the calls to {}".format(targets))


def lookup_product_by_doi(doi):
    biblio = {"doi": doi}
    return lookup_product(**biblio)


def lookup_product(**biblio):
    my_pub = None
    if "doi" in biblio and biblio["doi"]:
        doi = clean_doi(biblio["doi"])
        my_pub = Pub.query.get(doi)
        if my_pub:
            # logger.info(u"found {} in pub db table!".format(my_pub.id))
            my_pub.reset_vars()
        else:
            raise NoDoiException
        #     my_pub = Crossref(**biblio)
        #     logger.info(u"didn't find {} in crossref db table".format(my_pub))

    return my_pub



def refresh_pub(my_pub, do_commit=False):
    my_pub.run_with_hybrid()
    db.session.merge(my_pub)
    if do_commit:
        safe_commit(db)
    return my_pub


def thread_result_wrapper(func, args, res):
    res.append(func(*args))


# get rid of this when we get rid of POST endpoint
# for now, simplify it so it just calls the single endpoint
def get_pubs_from_biblio(biblios, run_with_hybrid=False):
    returned_pubs = []
    for biblio in biblios:
        returned_pubs.append(get_pub_from_biblio(biblio, run_with_hybrid=run_with_hybrid))
    return returned_pubs


def get_pub_from_biblio(biblio, run_with_hybrid=False, skip_all_hybrid=False):
    my_pub = lookup_product(**biblio)
    if run_with_hybrid:
        my_pub.run_with_hybrid()
        safe_commit(db)
    else:
        my_pub.recalculate(green_scrape_if_necessary=False)
    return my_pub

def max_pages_from_one_repo(repo_ids):
    repo_id_counter = Counter(repo_ids)
    most_common = repo_id_counter.most_common(1)
    if most_common:
        return most_common[0][1]
    return None

def get_citeproc_date(year=0, month=1, day=1):
    try:
        return datetime.datetime(year, month, day).isoformat()
    except ValueError:
        return None

def csv_dict_from_response_dict(data):
    if not data:
        return None

    response = defaultdict(str)
    response["doi"] = data.get("doi", None)
    response["doi_url"] = data.get("doi_url", None)
    response["is_oa"] = data.get("is_oa", None)
    response["genre"] = data.get("genre", None)
    response["journal_name"] = data.get("journal_name", None)
    response["journal_issns"] = data.get("journal_issns", None)
    response["journal_is_oa"] = data.get("journal_is_oa", None)
    response["publisher"] = data.get("publisher", None)
    response["published_date"] = data.get("published_date", None)
    response["data_standard"] = data.get("data_standard", None)

    best_location_data = data.get("best_oa_location", None)
    if not best_location_data:
        best_location_data = defaultdict(str)
    response["best_oa_url"] = best_location_data.get("url", "")
    response["best_oa_url_is_pdf"] = best_location_data.get("url_for_pdf", "") != ""
    response["best_oa_evidence"] = best_location_data.get("evidence", None)
    response["best_oa_host"] = best_location_data.get("host_type", None)
    response["best_oa_version"] = best_location_data.get("version", None)
    response["best_oa_license"] = best_location_data.get("license", None)

    return response



def build_crossref_record(data):
    if not data:
        return None

    record = {}

    simple_fields = [
        "publisher",
        "subject",
        "link",
        "license",
        "funder",
        "type",
        "update-to",
        "clinical-trial-number",
        "ISSN",  # needs to be uppercase
        "ISBN",  # needs to be uppercase
        "alternative-id"
    ]

    for field in simple_fields:
        if field in data:
            record[field.lower()] = data[field]

    if "title" in data:
        if isinstance(data["title"], basestring):
            record["title"] = data["title"]
        else:
            if data["title"]:
                record["title"] = data["title"][0]  # first one
        if "title" in record and record["title"]:
            record["title"] = re.sub(u"\s+", u" ", record["title"])


    if "container-title" in data:
        record["all_journals"] = data["container-title"]
        if isinstance(data["container-title"], basestring):
            record["journal"] = data["container-title"]
        else:
            if data["container-title"]:
                record["journal"] = data["container-title"][-1] # last one
        # get rid of leading and trailing newlines
        if record.get("journal", None):
            record["journal"] = record["journal"].strip()

    if "author" in data:
        # record["authors_json"] = json.dumps(data["author"])
        record["all_authors"] = data["author"]
        if data["author"]:
            first_author = data["author"][0]
            if first_author and u"family" in first_author:
                record["first_author_lastname"] = first_author["family"]
            for author in record["all_authors"]:
                if author and "affiliation" in author and not author.get("affiliation", None):
                    del author["affiliation"]


    if "issued" in data:
        # record["issued_raw"] = data["issued"]
        try:
            if "raw" in data["issued"]:
                record["year"] = int(data["issued"]["raw"])
            elif "date-parts" in data["issued"]:
                record["year"] = int(data["issued"]["date-parts"][0][0])
                date_parts = data["issued"]["date-parts"][0]
                pubdate = get_citeproc_date(*date_parts)
                if pubdate:
                    record["pubdate"] = pubdate
        except (IndexError, TypeError):
            pass

    if "deposited" in data:
        try:
            record["deposited"] = data["deposited"]["date-time"]
        except (IndexError, TypeError):
            pass


    record["added_timestamp"] = datetime.datetime.utcnow().isoformat()
    return record




class PmcidPublishedVersionLookup(db.Model):
    pmcid = db.Column(db.Text, db.ForeignKey('pmcid_lookup.pmcid'), primary_key=True)



class PmcidLookup(db.Model):
    doi = db.Column(db.Text, db.ForeignKey('pub.id'), primary_key=True)
    pmcid = db.Column(db.Text)
    release_date = db.Column(db.Text)

    pmcid_pubished_version_link = db.relationship(
        'PmcidPublishedVersionLookup',
        lazy='subquery',
        viewonly=True,
        cascade="all, delete-orphan",
        backref=db.backref("pmcid_lookup", lazy="subquery"),
        foreign_keys="PmcidPublishedVersionLookup.pmcid"
    )

    @property
    def version(self):
        if self.pmcid_pubished_version_link:
            return "publishedVersion"
        return "acceptedVersion"


class Pub(db.Model):
    id = db.Column(db.Text, primary_key=True)
    updated = db.Column(db.DateTime)
    crossref_api_raw_new = db.Column(JSONB)
    published_date = db.Column(db.DateTime)
    title = db.Column(db.Text)
    normalized_title = db.Column(db.Text)
    issns_jsonb = db.Column(JSONB)

    last_changed_date = db.Column(db.DateTime)
    response_jsonb = db.Column(JSONB)
    response_is_oa = db.Column(db.Boolean)
    response_best_evidence = db.Column(db.Text)
    response_best_url = db.Column(db.Text)
    response_best_host = db.Column(db.Text)
    response_best_repo_id = db.Column(db.Text)
    response_best_version = db.Column(db.Text)

    scrape_updated = db.Column(db.DateTime)
    scrape_evidence = db.Column(db.Text)
    scrape_pdf_url = db.Column(db.Text)
    scrape_metadata_url = db.Column(db.Text)
    scrape_license = db.Column(db.Text)

    error = db.Column(db.Text)

    started = db.Column(db.DateTime)
    finished = db.Column(db.DateTime)
    rand = db.Column(db.Numeric)

    # abstracts = db.relationship(
    #     'Abstract',
    #     lazy='subquery',
    #     viewonly=True,
    #     cascade="all, delete-orphan",
    #     backref=db.backref("pub", lazy="subquery"),
    #     foreign_keys="Abstract.doi"
    # )

    pmcid_links = db.relationship(
        'PmcidLookup',
        lazy='subquery',
        viewonly=True,
        cascade="all, delete-orphan",
        backref=db.backref("pub", lazy="subquery"),
        foreign_keys="PmcidLookup.doi"
    )

    page_matches_by_doi = db.relationship(
        'Page',
        lazy='subquery',
        cascade="all, delete-orphan",
        viewonly=True,
        enable_typechecks=False,
        backref=db.backref("pub_by_doi", lazy="subquery"),
        foreign_keys="Page.doi"
    )

    page_new_matches_by_doi = db.relationship(
        'PageDoiMatch',
        lazy='subquery',
        cascade="all, delete-orphan",
        viewonly=True,
        enable_typechecks=False,
        backref=db.backref("pub", lazy="subquery"),
        foreign_keys="PageDoiMatch.doi"
    )

    page_new_matches_by_title = db.relationship(
        'PageTitleMatch',
        lazy='subquery',
        cascade="all, delete-orphan",
        viewonly=True,
        enable_typechecks=False,
        backref=db.backref("pub", lazy="subquery"),
        foreign_keys="PageTitleMatch.normalized_title"
    )

    def __init__(self, **biblio):
        self.reset_vars()
        self.rand = random.random()
        # self.updated = datetime.datetime.utcnow()
        for (k, v) in biblio.iteritems():
            self.__setattr__(k, v)

    @orm.reconstructor
    def init_on_load(self):
        self.reset_vars()


    def reset_vars(self):
        if self.id and self.id.startswith("10."):
            self.id = clean_doi(self.id)

        self.license = None
        self.free_metadata_url = None
        self.free_pdf_url = None
        self.fulltext_url = None
        self.oa_color = None
        self.evidence = None
        self.open_locations = []
        self.closed_urls = []
        self.session_id = None
        self.version = None

    @property
    def doi(self):
        return self.id

    @property
    def unpaywall_api_url(self):
        return u"https://api.unpaywall.org/v2/{}?email=internal@impactstory.org".format(self.id)

    @property
    def tdm_api(self):
        return None

    @property
    def crossref_api_raw(self):
        record = None
        try:
            if self.crossref_api_raw_new:
                return self.crossref_api_raw_new
        except IndexError:
            pass

        return record

    @property
    def crossref_api_modified(self):
        record = None
        if self.crossref_api_raw_new:
            try:
                return build_crossref_record(self.crossref_api_raw_new)
            except IndexError:
                pass


        if self.crossref_api_raw:
            try:
                record = build_crossref_record(self.crossref_api_raw)
                print "got record"
                return record
            except IndexError:
                pass

        return record


    @property
    def open_urls(self):
        # return sorted urls, without dups
        urls = []
        for location in self.sorted_locations:
            if location.best_url not in urls:
                urls.append(location.best_url)
        return urls
    
    @property
    def url(self):
        return u"https://doi.org/{}".format(self.id)

    @property
    def is_oa(self):
        if self.fulltext_url:
            return True
        return False



    def recalculate(self, quiet=False, green_scrape_if_necessary=True):
        self.clear_locations()

        if self.publisher == "CrossRef Test Account":
            self.error += "CrossRef Test Account"
            raise NoDoiException

        self.find_open_locations(green_scrape_if_necessary)

        if self.is_oa and not quiet:
            logger.info(u"**REFRESH found a fulltext_url for {}!  {}: {} **".format(
                self.id, self.oa_status, self.fulltext_url))

    def refresh_crossref(self):
        from put_crossref_in_db import get_api_for_one_doi
        self.crossref_api_raw_new = get_api_for_one_doi(self.doi)

    def refresh_including_crossref(self):
        self.refresh_crossref()
        return self.refresh()

    def refresh(self, session_id=None):
        if session_id:
            self.session_id = session_id
        else:
            self.session_id = get_session_id()

        # self.refresh_green_locations()

        self.refresh_hybrid_scrape()

        # and then recalcualte everything, so can do to_dict() after this and it all works
        self.update()

        # then do this so the recalculated stuff saves
        # it's ok if this takes a long time... is a short time compared to refresh_hybrid_scrape
        db.session.merge(self)



    def set_results(self):
        self.issns_jsonb = self.issns
        self.response_jsonb = self.to_dict_v2()
        self.response_is_oa = self.is_oa
        self.response_best_url = self.best_url
        self.response_best_evidence = self.best_evidence
        self.response_best_version = self.best_version
        self.response_best_host = self.best_host
        self.response_best_repo_id = self.best_repo_id

    def clear_results(self):
        self.response_jsonb = None
        self.response_is_oa = None
        self.response_best_url = None
        self.response_best_evidence = None
        self.response_best_version = None
        self.response_best_host = None
        self.response_best_repo_id = None
        self.error = ""
        self.issns_jsonb = None


    def has_changed(self, old_response_jsonb):
        # for now at least, or else too noisy on all the plos components
        if self.genre == "component":
            return False

        if not old_response_jsonb:
            logger.info(u"response for {} has changed: no old response".format(self.id))
            return True

        copy_of_new_response = self.response_jsonb
        copy_of_old_response = old_response_jsonb

        copy_of_new_response_in_json = json.dumps(copy_of_new_response, sort_keys=True, indent=2)  # have to sort to compare
        copy_of_old_response_in_json = json.dumps(copy_of_old_response, sort_keys=True, indent=2)  # have to sort to compare

        # remove these keys because they may change
        keys_to_delete = ["updated", "last_changed_date", "x_reported_noncompliant_copies", "x_error", "data_standard"]
        for key in keys_to_delete:
            # also remove it if it is an empty list
            copy_of_new_response_in_json = re.sub(ur'"{}":\s*".+?",?\s*'.format(key), '', copy_of_new_response_in_json)
            copy_of_old_response_in_json = re.sub(ur'"{}":\s*".+?",?\s*'.format(key), '', copy_of_old_response_in_json)

            # also remove it if it is an empty list
            copy_of_new_response_in_json = re.sub(ur'"{}":\s*\[\],?\s*'.format(key), '', copy_of_new_response_in_json)
            copy_of_old_response_in_json = re.sub(ur'"{}":\s*\[\],?\s*'.format(key), '', copy_of_old_response_in_json)

            # also anything till a comma (gets data_standard)
            copy_of_new_response_in_json = re.sub(ur'"{}":\s*.+?,\s*'.format(key), '', copy_of_new_response_in_json)
            copy_of_old_response_in_json = re.sub(ur'"{}":\s*.+?,\s*'.format(key), '', copy_of_old_response_in_json)

        if copy_of_new_response_in_json != copy_of_old_response_in_json:
            # print "copy_of_new_response_in_json, ***"
            # print copy_of_new_response_in_json
            # print "copy_of_old_response_in_json, ***"
            # print copy_of_old_response_in_json
            return True

        return False

    def update(self):
        return self.recalculate_and_store()

    def recalculate_and_store(self):
        if not self.crossref_api_raw_new:
            self.crossref_api_raw_new = self.crossref_api_raw

        if not self.title:
            self.title = self.crossref_title
        self.normalized_title = normalize_title(self.title)
        if not self.published_date:
            self.published_date = self.issued
        if not self.rand:
            self.rand = random.random()

        old_response_jsonb = self.response_jsonb

        self.clear_results()
        try:
            self.recalculate()
        except NoDoiException:
            logger.info(u"invalid doi {}".format(self))
            self.error += "Invalid DOI"
            pass

        self.set_results()

        if self.has_changed(old_response_jsonb):
            logger.info(u"changed! updating the pub table for this record! {}".format(self.id))
            self.last_changed_date = datetime.datetime.utcnow().isoformat()
            self.updated = datetime.datetime.utcnow()
            flag_modified(self, "response_jsonb") # force it to be saved
        else:
            # logger.info(u"didn't change")
            pass


        # after recalculate, so can know if is open
        # self.set_abstracts()



    def run(self):
        try:
            self.recalculate_and_store()
        except NoDoiException:
            logger.info(u"invalid doi {}".format(self))
            self.error += "Invalid DOI"
            pass
        # logger.info(json.dumps(self.response_jsonb, indent=4))


    def run_with_hybrid(self, quiet=False, shortcut_data=None):
        logger.info(u"in run_with_hybrid")
        self.clear_results()
        try:
            self.refresh()
        except NoDoiException:
            logger.info(u"invalid doi {}".format(self))
            self.error += "Invalid DOI"
            pass

        # set whether changed or not
        self.set_results()


    @property
    def has_been_run(self):
        if self.evidence:
            return True
        return False

    @property
    def best_redirect_url(self):
        if self.fulltext_url:
            return self.fulltext_url
        else:
            return self.url

    @property
    def has_fulltext_url(self):
        return (self.fulltext_url != None)

    @property
    def has_license(self):
        if not self.license:
            return False
        if self.license == "unknown":
            return False
        return True

    @property
    def clean_doi(self):
        if not self.id:
            return None
        return clean_doi(self.id)

    def ask_manual_overrides(self):
        if not self.doi:
            return

        override_dict = oa_manual.get_overrides_dict()
        if self.doi in override_dict:
            logger.info(u"manual override for {}".format(self.doi))
            self.open_locations = []
            if override_dict[self.doi]:
                my_location = OpenLocation()
                my_location.pdf_url = None
                my_location.metadata_url = None
                my_location.license = None
                my_location.version = None
                my_location.evidence = "manual"
                my_location.doi = self.doi

                # set just what the override dict specifies
                for (k, v) in override_dict[self.doi].iteritems():
                    setattr(my_location, k, v)

                # don't append, make it the only one
                self.open_locations.append(my_location)


    def set_fulltext_url(self):
        # give priority to pdf_url
        self.fulltext_url = None
        if self.free_pdf_url:
            self.fulltext_url = self.free_pdf_url
        elif self.free_metadata_url:
            self.fulltext_url = self.free_metadata_url


    def decide_if_open(self):
        # look through the locations here

        # overwrites, hence the sorting
        self.license = None
        self.free_metadata_url = None
        self.free_pdf_url = None
        self.oa_color = None
        self.version = None
        self.evidence = None

        reversed_sorted_locations = self.sorted_locations
        reversed_sorted_locations.reverse()

        # go through all the locations, using valid ones to update the best open url data
        for location in reversed_sorted_locations:
            self.free_pdf_url = location.pdf_url
            self.free_metadata_url = location.metadata_url
            self.evidence = location.evidence
            self.oa_color = location.oa_color
            self.version = location.version
            self.license = location.license

        self.set_fulltext_url()

        # don't return an open license on a closed thing, that's confusing
        if not self.fulltext_url:
            self.license = None
            self.evidence = None
            self.oa_color = None
            self.version = None


    @property
    def oa_status(self):
        if self.oa_color == "green":
            return "green"
        if self.oa_color == "gold":
            return "gold"
        if self.oa_color in ["blue", "bronze"]:
            return "bronze"
        return "closed"


    def clear_locations(self):
        self.reset_vars()



    @property
    def has_hybrid(self):
        return any([location.is_hybrid for location in self.open_locations])

    @property
    def has_gold(self):
        return any([location.oa_color=="gold" for location in self.open_locations])

    def refresh_green_locations(self):
        for my_page in self.pages:
            my_page.scrape()


    def refresh_hybrid_scrape(self):
        logger.info(u"***** {}: {}".format(self.publisher, self.journal))
        # look for hybrid
        self.scrape_updated = datetime.datetime.utcnow().isoformat()

        # reset
        self.scrape_evidence = None
        self.scrape_pdf_url = None
        self.scrape_metadata_url = None
        self.scrape_license = None

        if self.url:
            with PublisherWebpage(url=self.url,
                                  related_pub_doi=self.doi,
                                  related_pub_publisher=self.publisher,
                                  session_id=self.session_id) as publisher_landing_page:

                # end the session before the scrape
                # logger.info(u"closing session for {}".format(self.doi))
                db.session.close()

                self.scrape_page_for_open_location(publisher_landing_page)

                # now merge our object back in
                # logger.info(u"after scrape, merging {}".format(self.doi))
                db.session.merge(self)

                if publisher_landing_page.is_open:
                    self.scrape_evidence = publisher_landing_page.open_version_source_string
                    self.scrape_pdf_url = publisher_landing_page.scraped_pdf_url
                    self.scrape_metadata_url = publisher_landing_page.scraped_open_metadata_url
                    self.scrape_license = publisher_landing_page.scraped_license
                    if publisher_landing_page.is_open and not publisher_landing_page.scraped_pdf_url:
                        self.scrape_metadata_url = self.url
        return


    def find_open_locations(self, green_scrape_if_necessary=True):

        # just based on doi
        self.ask_local_lookup()
        self.ask_pmc()

        # based on titles
        self.set_title_hacks()  # has to be before ask_green_locations, because changes titles

        self.ask_green_locations(green_scrape_if_necessary)
        self.ask_hybrid_scrape()
        self.ask_manual_overrides()

        # now consolidate
        self.decide_if_open()
        self.set_license_hacks()  # has to be after ask_green_locations, because uses repo names


    def ask_local_lookup(self):
        start_time = time()

        evidence = None
        fulltext_url = self.url

        license = None
        pdf_url = None
        version = "publishedVersion"  #default

        if oa_local.is_open_via_doaj(self.issns, self.all_journals, self.year):
            license = oa_local.is_open_via_doaj(self.issns, self.all_journals, self.year)
            evidence = "oa journal (via doaj)"
        elif oa_local.is_open_via_publisher(self.publisher):
            evidence = "oa journal (via publisher name)"
        elif oa_local.is_open_via_doi_fragment(self.doi):
            evidence = "oa repository (via doi prefix)"
        elif oa_local.is_open_via_url_fragment(self.url):
            evidence = "oa repository (via url prefix)"
        elif oa_local.is_open_via_license_urls(self.crossref_license_urls):
            freetext_license = oa_local.is_open_via_license_urls(self.crossref_license_urls)
            license = oa_local.find_normalized_license(freetext_license)
            # logger.info(u"freetext_license: {} {}".format(freetext_license, license))
            evidence = "open (via crossref license)"  # oa_color depends on this including the word "hybrid"
        elif self.open_manuscript_license_urls:
            freetext_license = self.open_manuscript_license_urls[0]
            license = oa_local.find_normalized_license(freetext_license)
            if freetext_license and not license:
                license = "publisher-specific, author manuscript: {}".format(freetext_license)
            version = "acceptedVersion"
            if self.is_same_publisher("Elsevier BV"):
                elsevier_id = self.crossref_alternative_id
                pdf_url = u"https://manuscript.elsevier.com/{}/pdf/{}.pdf".format(elsevier_id, elsevier_id)
            elif self.is_same_publisher("American Physical Society (APS)"):
                proper_case_id = self.id
                proper_case_id = proper_case_id.replace("revmodphys", "RevModPhys")
                proper_case_id = proper_case_id.replace("physrevlett", "PhysRevLett")
                proper_case_id = proper_case_id.replace("physreva", "PhysRevA")
                proper_case_id = proper_case_id.replace("physrevb", "PhysRevB")
                proper_case_id = proper_case_id.replace("physrevc", "PhysRevC")
                proper_case_id = proper_case_id.replace("physrevd", "PhysRevD")
                proper_case_id = proper_case_id.replace("physreve", "PhysRevE")
                proper_case_id = proper_case_id.replace("physrevx", "PhysRevX")
                proper_case_id = proper_case_id.replace("physrevaccelbeams", "PhysRevAccelBeams")
                proper_case_id = proper_case_id.replace("physrevapplied", "PhysRevApplied")
                proper_case_id = proper_case_id.replace("physrevphyseducres", "PhysRevPhysEducRes")
                proper_case_id = proper_case_id.replace("physrevstper", "PhysRevSTPER")
                if proper_case_id != self.id:
                    pdf_url = u"https://link.aps.org/accepted/{}".format(proper_case_id)
            elif self.is_same_publisher("AIP Publishing"):
                pdf_url = "https://aip.scitation.org/doi/{}".format(self.id)
            elif self.is_same_publisher("IOP Publishing"):
                pdf_url = "http://iopscience.iop.org/article/{}/ampdf".format(self.id)

            evidence = "open (via crossref license, author manuscript)"  # oa_color depends on this including the word "hybrid"
            # logger.info(u"freetext_license: {} {}".format(freetext_license, license))

        if evidence:
            my_location = OpenLocation()
            my_location.metadata_url = fulltext_url
            my_location.license = license
            my_location.evidence = evidence
            my_location.updated = datetime.datetime.utcnow()
            my_location.doi = self.doi
            my_location.version = version
            if pdf_url:
                my_location.pdf_url = pdf_url

            self.open_locations.append(my_location)

    def ask_pmc(self):
        total_start_time = time()

        for pmc_obj in self.pmcid_links:
            if pmc_obj.release_date == "live":
                my_location = OpenLocation()
                my_location.metadata_url = "https://www.ncbi.nlm.nih.gov/pmc/articles/{}".format(pmc_obj.pmcid.upper())
                # we don't know this has a pdf version
                # my_location.pdf_url = "https://www.ncbi.nlm.nih.gov/pmc/articles/{}/pdf".format(pmc_obj.pmcid.upper())
                my_location.evidence = "oa repository (via pmcid lookup)"
                my_location.updated = datetime.datetime.utcnow()
                my_location.doi = self.doi
                my_location.version = pmc_obj.version
                # set version in one central place for pmc right now, till refactor done
                self.open_locations.append(my_location)

    @property
    def has_stored_hybrid_scrape(self):
        return (self.scrape_evidence and self.scrape_evidence != "closed")

    def ask_hybrid_scrape(self):
        if self.has_stored_hybrid_scrape:
            my_location = OpenLocation()
            my_location.pdf_url = self.scrape_pdf_url
            my_location.metadata_url = self.scrape_metadata_url
            my_location.license = self.scrape_license
            my_location.evidence = self.scrape_evidence
            my_location.updated = self.scrape_updated
            my_location.doi = self.doi
            my_location.version = "publishedVersion"
            self.open_locations.append(my_location)


    @property
    def page_matches_by_doi_filtered(self):
        return self.page_matches_by_doi + self.page_new_matches_by_doi

    @property
    def page_matches_by_title_filtered(self):

        my_pages = []

        if not self.normalized_title:
            return my_pages

        for my_page in self.page_new_matches_by_title:
            # don't do this right now.  not sure if it helps or hurts.
            # don't check title match if we already know it belongs to a different doi
            # if my_page.doi and my_page.doi != self.doi:
            #     continue

            # double check author match
            match_type = "title"
            if self.first_author_lastname or self.last_author_lastname:
                if my_page.authors:
                    try:
                        pmh_author_string = u", ".join(my_page.authors)
                        if self.first_author_lastname and normalize(self.first_author_lastname) in normalize(pmh_author_string):
                            match_type = "title and first author"
                        elif self.last_author_lastname and normalize(self.last_author_lastname) in normalize(pmh_author_string):
                            match_type = "title and last author"
                        else:
                            # logger.info(u"author check fails, so skipping this record. Looked for {} and {} in {}".format(
                            #     self.first_author_lastname, self.last_author_lastname, pmh_author_string))
                            # logger.info(self.authors)
                            # don't match if bad author match
                            continue
                    except TypeError:
                        pass # couldn't make author string
            my_page.match_evidence = u"oa repository (via OAI-PMH {} match)".format(match_type)
            my_pages.append(my_page)
        return my_pages

    @property
    def pages(self):
        my_pages = []

        # @todo remove these checks once we are just using the new page
        if self.normalized_title:
            if title_is_too_short(self.normalized_title):
                # logger.info(u"title too short! don't match by title")
                pass
            elif title_is_too_common(self.normalized_title):
                # logger.info(u"title too common!  don't match by title.")
                pass
            else:
                my_pages = self.page_matches_by_title_filtered

        # do dois last, because the objects are actually the same, not copies, and then they get the doi reason
        for my_page in self.page_matches_by_doi_filtered:
            my_page.match_evidence = u"oa repository (via OAI-PMH doi match)"
            if not my_page.scrape_version and u"/pmc/" in my_page.url:
                my_page.set_info_for_pmc_page()

            my_pages.append(my_page)

        # eventually only apply this filter to matches by title, once pages only includes
        # the doi when it comes straight from the pmh record
        if max_pages_from_one_repo([p.repo_id for p in self.page_matches_by_title_filtered]) >= 10:
            my_pages = []
            logger.info(u"matched too many pages in one repo, not allowing matches")

        return my_pages

    def ask_green_locations(self, green_scrape_if_necessary=True):
        has_new_green_locations = False
        for my_page in self.pages:
            if green_scrape_if_necessary:
                if hasattr(my_page, "num_pub_matches") and (my_page.num_pub_matches == 0 or my_page.num_pub_matches is None):
                    my_page.num_pub_matches = 1  # update this so don't rescrape next time
                    dont_check_these_endpoints = ["open-archive.highwire.org/handler"]
                    if (my_page.error is None or my_page.error=="") and my_page.repo_id not in dont_check_these_endpoints:
                        logger.info(u"scraping green page num_pub_matches was 0 for {} {} {}".format(
                            self.id, my_page.repo_id, my_page.pmh_id))
                        my_page.scrape_if_matches_pub()

            if my_page.is_open:
                new_open_location = OpenLocation()
                new_open_location.pdf_url = my_page.scrape_pdf_url
                new_open_location.metadata_url = my_page.scrape_metadata_url
                new_open_location.license = my_page.scrape_license
                new_open_location.evidence = my_page.match_evidence
                new_open_location.version = my_page.scrape_version
                new_open_location.updated = my_page.scrape_updated
                new_open_location.doi = my_page.doi
                new_open_location.pmh_id = my_page.pmh_id
                new_open_location.repo_id = my_page.repo_id
                self.open_locations.append(new_open_location)
                has_new_green_locations = True
        return has_new_green_locations


    # comment out for now so that not scraping by accident
    # def scrape_these_pages(self, webpages):
    #     webpage_arg_list = [[page] for page in webpages]
    #     call_args_in_parallel(self.scrape_page_for_open_location, webpage_arg_list)


    def scrape_page_for_open_location(self, my_webpage):
        # logger.info(u"scraping", url)
        try:
            my_webpage.scrape_for_fulltext_link()

            if my_webpage.error:
                self.error += my_webpage.error

            if my_webpage.is_open:
                my_open_location = my_webpage.mint_open_location()
                self.open_locations.append(my_open_location)
                # logger.info(u"found open version at", webpage.url)
            else:
                # logger.info(u"didn't find open version at", webpage.url)
                pass

        except requests.Timeout, e:
            self.error += "Timeout in scrape_page_for_open_location on {}: {}".format(my_webpage, unicode(e.message).encode("utf-8"))
            logger.info(self.error)
        except requests.exceptions.ConnectionError, e:
            self.error += "ConnectionError in scrape_page_for_open_location on {}: {}".format(my_webpage, unicode(e.message).encode("utf-8"))
            logger.info(self.error)
        except requests.exceptions.ChunkedEncodingError, e:
            self.error += "ChunkedEncodingError in scrape_page_for_open_location on {}: {}".format(my_webpage, unicode(e.message).encode("utf-8"))
            logger.info(self.error)
        except requests.exceptions.RequestException, e:
            self.error += "RequestException in scrape_page_for_open_location on {}: {}".format(my_webpage, unicode(e.message).encode("utf-8"))
            logger.info(self.error)
        except etree.XMLSyntaxError, e:
            self.error += "XMLSyntaxError in scrape_page_for_open_location on {}: {}".format(my_webpage, unicode(e.message).encode("utf-8"))
            logger.info(self.error)
        except Exception, e:
            logger.exception(u"Exception in scrape_page_for_open_location")
            self.error += "Exception in scrape_page_for_open_location"
            logger.info(self.error)


    def set_title_hacks(self):
        workaround_titles = {
            # these preprints doesn't have the same title as the doi
            # eventually solve these by querying arxiv like this:
            # http://export.arxiv.org/api/query?search_query=doi:10.1103/PhysRevD.89.085017
            "10.1016/j.astropartphys.2007.12.004": "In situ radioglaciological measurements near Taylor Dome, Antarctica and implications for UHE neutrino astronomy",
            "10.1016/s0375-9601(02)01803-0": "Universal quantum computation using only projective measurement, quantum memory, and preparation of the 0 state",
            "10.1103/physreva.65.062312": "An entanglement monotone derived from Grover's algorithm",

            # crossref has title "aol" for this
            # set it to real title
            "10.1038/493159a": "Altmetrics: Value all research products",

            # crossref has no title for this
            "10.1038/23891": "Complete quantum teleportation using nuclear magnetic resonance",

            # is a closed-access datacite one, with the open-access version in BASE
            # need to set title here because not looking up datacite titles yet (because ususally open access directly)
            "10.1515/fabl.1988.29.1.21": u"Thesen zur Verabschiedung des Begriffs der 'historischen Sage'",

            # preprint has a different title
            "10.1123/iscj.2016-0037": u"METACOGNITION AND PROFESSIONAL JUDGMENT AND DECISION MAKING: IMPORTANCE, APPLICATION AND EVALUATION"
        }

        if self.doi in workaround_titles:
            self.title = workaround_titles[self.doi]


    def set_license_hacks(self):
        if self.fulltext_url and u"harvard.edu/" in self.fulltext_url:
            if not self.license or self.license=="unknown":
                self.license = "cc-by-nc"

    @property
    def crossref_alternative_id(self):
        try:
            return re.sub(u"\s+", " ", self.crossref_api_raw_new["alternative-id"][0])
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def publisher(self):
        try:
            return re.sub(u"\s+", " ", self.crossref_api_modified["publisher"])
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def issued(self):
        try:
            if self.crossref_api_raw_new and "date-parts" in self.crossref_api_raw_new["issued"]:
                date_parts = self.crossref_api_raw_new["issued"]["date-parts"][0]
                issued_date = get_citeproc_date(*date_parts)
                if issued_date:
                    return issued_date[0:10]
        except (KeyError, TypeError, AttributeError):
            return None


    @property
    def open_manuscript_license_urls(self):
        try:
            license_dicts = self.crossref_api_modified["license"]
            author_manuscript_urls = []

            # only include licenses that are past the start date
            for license_dict in license_dicts:
                if license_dict.get("content-version", None):
                    if license_dict["content-version"] == u"am":
                        valid_now = True
                        if license_dict.get("start", None):
                            if license_dict["start"].get("date-time", None):
                                if license_dict["start"]["date-time"] > datetime.datetime.utcnow().isoformat():
                                    valid_now = False
                        if valid_now:
                            author_manuscript_urls.append(license_dict["URL"])

            return author_manuscript_urls
        except (KeyError, TypeError):
            return []

    @property
    def crossref_license_urls(self):
        try:
            license_dicts = self.crossref_api_modified["license"]
            license_urls = []

            # only include licenses that are past the start date
            for license_dict in license_dicts:
                if license_dict.get("content-version", None):
                    if license_dict["content-version"] == u"vor":
                        valid_now = True
                        if license_dict.get("start", None):
                            if license_dict["start"].get("date-time", None):
                                if license_dict["start"]["date-time"] > datetime.datetime.utcnow().isoformat():
                                    valid_now = False
                        if valid_now:
                            license_urls.append(license_dict["URL"])

            return license_urls
        except (KeyError, TypeError):
            return []

    @property
    def is_subscription_journal(self):
        if oa_local.is_open_via_doaj(self.issns, self.all_journals, self.year) \
            or oa_local.is_open_via_doi_fragment(self.doi) \
            or oa_local.is_open_via_publisher(self.publisher) \
            or oa_local.is_open_via_url_fragment(self.url):
                return False
        return True


    @property
    def doi_resolver(self):
        if not self.doi:
            return None
        if oa_local.is_open_via_datacite_prefix(self.doi):
            return "datacite"
        if self.crossref_api_modified and not "error" in self.crossref_api_modified:
            return "crossref"
        return None

    @property
    def is_free_to_read(self):
        if self.fulltext_url:
            return True
        return False

    @property
    def is_boai_license(self):
        boai_licenses = ["cc-by", "cc0", "pd"]
        if self.license and (self.license in boai_licenses):
            return True
        return False

    @property
    def authors(self):
        try:
            return self.crossref_api_modified["all_authors"]
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def first_author_lastname(self):
        try:
            return self.crossref_api_modified["first_author_lastname"]
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def last_author_lastname(self):
        try:
            last_author = self.authors[-1]
            return last_author["family"]
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def display_issns(self):
        if self.issns:
            return ",".join(self.issns)
        return None

    @property
    def issns(self):
        issns = []
        try:
            issns = self.crossref_api_modified["issn"]
        except (AttributeError, TypeError, KeyError):
            try:
                issns = self.crossref_api_modified["issn"]
            except (AttributeError, TypeError, KeyError):
                if self.tdm_api:
                    issns = re.findall(u"<issn media_type=.*>(.*)</issn>", self.tdm_api)
        if not issns:
            return None
        else:
            return issns

    @property
    def best_title(self):
        if hasattr(self, "title") and self.title:
            return re.sub(u"\s+", " ", self.title)
        return self.crossref_title

    @property
    def crossref_title(self):
        try:
            return re.sub(u"\s+", " ", self.crossref_api_modified["title"])
        except (AttributeError, TypeError, KeyError, IndexError):
            return None

    @property
    def year(self):
        try:
            return self.crossref_api_modified["year"]
        except (AttributeError, TypeError, KeyError, IndexError):
            return None

    @property
    def journal(self):
        try:
            return re.sub(u"\s+", " ", self.crossref_api_modified["journal"])
        except (AttributeError, TypeError, KeyError, IndexError):
            return None

    @property
    def all_journals(self):
        try:
            return self.crossref_api_modified["all_journals"]
        except (AttributeError, TypeError, KeyError, IndexError):
            return None

    @property
    def genre(self):
        try:
            return re.sub(u"\s+", " ", self.crossref_api_modified["type"])
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def abstract_from_crossref(self):
        try:
            return self.crossref_api_raw_new["abstract"]
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def deduped_sorted_locations(self):
        locations = []
        for next_location in self.sorted_locations:
            urls_so_far = [location.best_url for location in locations]
            if next_location.best_url not in urls_so_far:
                locations.append(next_location)
        return locations

    @property
    def sorted_locations(self):
        locations = self.open_locations
        # first sort by best_url so ties are handled consistently
        locations = sorted(locations, key=lambda x: x.best_url, reverse=False)
        # now sort by what's actually better
        locations = sorted(locations, key=lambda x: x.sort_score, reverse=False)

        # now remove noncompliant ones
        locations = [location for location in locations if not location.is_reported_noncompliant]
        return locations

    @property
    def data_standard(self):
        # if self.scrape_updated or self.oa_status in ["gold", "hybrid", "bronze"]:
        if self.scrape_updated and not self.error:
            return 2
        else:
            return 1

    def get_resolved_url(self):
        if hasattr(self, "my_resolved_url_cached"):
            return self.my_resolved_url_cached
        try:
            r = requests.get("http://doi.org/{}".format(self.id),
                             stream=True,
                             allow_redirects=True,
                             timeout=(3,3),
                             verify=False
                )

            self.my_resolved_url_cached = r.url

        except Exception:  #hardly ever do this, but man it seems worth it right here
            logger.exception(u"get_resolved_url failed")
            self.my_resolved_url_cached = None

        return self.my_resolved_url_cached

    def __repr__(self):
        if self.id:
            my_string = self.id
        else:
            my_string = self.best_title
        return u"<Pub ( {} )>".format(my_string)

    @property
    def reported_noncompliant_copies(self):
        return reported_noncompliant_url_fragments(self.doi)

    def is_same_publisher(self, publisher):
        if self.publisher:
            return normalize(self.publisher) == normalize(publisher)
        return False

    @property
    def best_url(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.best_url

    @property
    def best_url_is_pdf(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.best_url_is_pdf

    @property
    def best_evidence(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.display_evidence

    @property
    def best_host(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.host_type

    @property
    def best_repo_id(self):
        if self.best_host != 'repository':
            return None
        return self.best_oa_location.repo_id


    @property
    def best_license(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.license

    @property
    def best_version(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.version


    @property
    def best_oa_location_dict(self):
        best_location = self.best_oa_location
        if best_location:
            return best_location.to_dict_v2()
        return None

    @property
    def best_oa_location(self):
        all_locations = [location for location in self.all_oa_locations]
        if all_locations:
            return all_locations[0]
        return None

    @property
    def all_oa_locations(self):
        all_locations = [location for location in self.deduped_sorted_locations]
        if all_locations:
            for location in all_locations:
                location.is_best = False
            all_locations[0].is_best = True
        return all_locations

    def all_oa_location_dicts(self):
        return [location.to_dict_v2() for location in self.all_oa_locations]

    def to_dict_v1(self):
        response = {
            "algorithm_version": self.data_standard,
            "doi_resolver": self.doi_resolver,
            "evidence": self.evidence,
            "free_fulltext_url": self.fulltext_url,
            "is_boai_license": self.is_boai_license,
            "is_free_to_read": self.is_free_to_read,
            "is_subscription_journal": self.is_subscription_journal,
            "license": self.license,
            "oa_color": self.oa_color,
            "reported_noncompliant_copies": self.reported_noncompliant_copies
        }

        for k in ["doi", "title", "url"]:
            value = getattr(self, k, None)
            if value:
                response[k] = value

        if self.error:
            response["error"] = self.error

        return response

    @property
    def best_location(self):
        if not self.deduped_sorted_locations:
            return None
        return self.deduped_sorted_locations[0]

    @property
    def is_archived_somewhere(self):
        if self.is_oa:
            return any([location.oa_color=="green" for location in self.deduped_sorted_locations])
        return None

    @property
    def oa_is_doaj_journal(self):
        if self.is_oa:
            if oa_local.is_open_via_doaj(self.issns, self.all_journals, self.year):
                return True
            else:
                return False
        return False

    @property
    def oa_is_open_journal(self):
        if self.is_oa:
            if self.oa_is_doaj_journal:
                return True
            if oa_local.is_open_via_publisher(self.publisher):
                return True
        return False

    @property
    def display_updated(self):
        if self.updated:
            return self.updated.isoformat()
        return None

    @property
    def has_abstract(self):
        if self.abstracts:
            return True
        return False

    @property
    def display_abstracts(self):
        # self.set_abstracts()
        return [a.to_dict() for a in self.abstracts]


    def set_abstracts(self):
        start_time = time()

        abstract_objects = []

        # already have abstracts, don't keep trying
        if self.abstracts:
            logger.info(u"already had abstract stored!")
            return

        # try locally first
        if self.abstract_from_crossref:
            abstract_objects.append(Abstract(source="crossref", source_id=self.doi, abstract=self.abstract_from_crossref, doi=self.id))

        pmh_ids = [p.pmh_id for p in self.pages if p.pmh_id]
        if pmh_ids:
            pmh_records = db.session.query(PmhRecord).filter(PmhRecord.id.in_(pmh_ids)).all()
            for pmh_record in pmh_records:
                api_contents = pmh_record.api_raw.replace("\n", " ")
                matches = re.findall(u"<dc:description>(.*?)</dc:description>", api_contents, re.IGNORECASE | re.MULTILINE)
                if matches:
                    concat_description = u"\n".join(matches).strip()
                    abstract_objects.append(Abstract(source="pmh", source_id=pmh_record.id, abstract=concat_description, doi=self.id))

        # the more time consuming checks, only do them if the paper is open and recent for now
        # if self.is_oa and self.year and self.year == 2018:
        if self.is_oa and self.year and self.year >= 2017:

            # if nothing yet, query pmc with doi
            if not abstract_objects:
                result_list = query_pmc(self.id)
                for result in result_list:
                    if result.get("doi", None) == self.id:
                        pmid = result.get("pmid", None)
                        if u"abstractText" in result:
                            abstract_text = result["abstractText"]
                            abstract_obj = Abstract(source="pubmed", source_id=pmid, abstract=abstract_text, doi=self.id)
                            try:
                                abstract_obj.mesh = result["meshHeadingList"]["meshHeading"]
                            except KeyError:
                                pass
                            try:
                                abstract_obj.keywords = result["keywordList"]["keyword"]
                            except KeyError:
                                pass
                            abstract_objects.append(abstract_obj)
                            logger.info(u"got abstract from pubmed")

            if not abstract_objects:
                from oa_mendeley import query_mendeley
                result = query_mendeley(self.id)
                if result and result["abstract"]:
                    mendeley_url = result["mendeley_url"]
                    abstract_obj = Abstract(source="mendeley", source_id=mendeley_url, abstract=result["abstract"], doi=self.id)
                    abstract_objects.append(abstract_obj)
                    logger.info(u"GOT abstract from mendeley for {}".format(self.id))
                else:
                    logger.info(u"no abstract in mendeley for {}".format(self.id))


            logger.info(u"spent {} seconds getting abstracts for {}, success: {}".format(elapsed(start_time), self.id, len(abstract_objects)>0))

        # make sure to save what we got
        for abstract in abstract_objects:
            if abstract.source_id not in [a.source_id for a in self.abstracts]:
                self.abstracts.append(abstract)



    def to_dict_v2(self):
        response = {
            "doi": self.doi,
            "doi_url": self.url,
            "is_oa": self.is_oa,
            "best_oa_location": self.best_oa_location_dict,
            "oa_locations": self.all_oa_location_dicts(),
            "data_standard": self.data_standard,
            "title": self.best_title,
            "year": self.year,
            "journal_is_oa": self.oa_is_open_journal,
            "journal_is_in_doaj": self.oa_is_doaj_journal,
            "journal_issns": self.display_issns,
            "journal_name": self.journal,
            "publisher": self.publisher,
            "published_date": self.issued,
            "updated": self.display_updated,
            "genre": self.genre,
            "z_authors": self.authors,

            # "abstracts": self.display_abstracts,

        }

        # if self.error:
        #     response["x_error"] = True

        return response


    def to_dict_search(self):

        response = self.to_dict_v2()

        response["abstracts"] = self.display_abstracts

        del response["z_authors"]
        if self.authors:
            response["author_lastnames"] = [author.get("family", None) for author in self.authors]
        else:
            response["author_lastnames"] = []

        if not hasattr(self, "score"):
            self.score = None
        response["score"] = self.score

        if not hasattr(self, "snippet"):
            self.snippet = None
        response["snippet"] = self.snippet


        return response




# db.create_all()
# commit_success = safe_commit(db)
# if not commit_success:
#     logger.info(u"COMMIT fail making objects")
