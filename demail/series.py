from elasticsearch import Elasticsearch
from newman.newman_config import elasticsearch_hosts
from time import gmtime, strftime
import tangelo
from newman.newman_config import default_min_timeline_bound, default_max_timeline_bound, default_timeline_span, default_timeline_interval
from es_queries import _build_filter, _addrs_filter, _date_filter
from dateutil.parser import parse
from datetime import timedelta

def _date_aggs(date_field="datetime"):
    return {
        "min_date" : { "min" : { "field" : date_field } },
        "max_date" : { "max" : { "field" : date_field } },
        "avg_date" : { "avg" : { "field" : date_field } },
        "pct_date" : { "percentiles" : { "field" : date_field } }
    }

def get_datetime_bounds(index, type="emails"):
    es = Elasticsearch(elasticsearch_hosts())
    resp = es.search(index=index, doc_type=type, body={"aggregations":_date_aggs()})

    now = strftime("%Y-%m-%d", gmtime())
    min = resp["aggregations"]["min_date"].get("value_as_string", default_min_timeline_bound())
    max = resp["aggregations"]["max_date"].get("value_as_string", default_max_timeline_bound())

    # Average
    avg = resp["aggregations"]["avg_date"].get("value_as_string", None)
    # Estimated median
    pct = resp["aggregations"]["pct_date"]["values"].get("50.0_as_string", None)

    if not pct:
        return  (min if min >= "1970" else "1970-01-01", max if max <= now else now)

    avg_datetime = parse(pct)

    delta = timedelta(**{default_timeline_interval() : int(default_timeline_span())/2})

    return ((avg_datetime-delta).strftime("%Y-%m-%d"), (avg_datetime+delta).strftime("%Y-%m-%d"))

def _map_attachments(index, account_id, attchments):
    return {"account_id" : account_id,
            "interval_start_datetime" : attchments[0]["key_as_string"],
            "interval_attach_count" : attchments[0]["doc_count"]
            }

def _map_activity(index, account_id, sent_rcvd):
    return {"account_id" : account_id,
            "interval_start_datetime" : sent_rcvd[0]["key_as_string"],
            "interval_inbound_count" : sent_rcvd[0]["doc_count"],
            "interval_outbound_count" : sent_rcvd[1]["doc_count"]
            }

def entity_histogram_query(email_addrs=[], query_terms='', topic_score=None, date_bounds=None, entity_agg_size=10):
    return {"aggs" : {
        "filtered_entity_agg" : {
            "filter" : _build_filter(email_senders=email_addrs, email_rcvrs=email_addrs, query_terms=query_terms, date_bounds=date_bounds),
            "aggs": {
                "person" : {
                    "terms" : {"field" : "entities.entity_person", "size": entity_agg_size}
                },
                "organization" : {
                    "terms" : {"field" : "entities.entity_organization", "size": entity_agg_size}
                },
                "location" : {
                    "terms" : {"field" : "entities.entity_location", "size": entity_agg_size}
                },
                "misc" : {
                    "terms" : {"field" : "entities.mics", "size": entity_agg_size}
                }

            }
        }}, "size":0}


def get_entity_histogram(index, type, email_addrs=[], query_terms='', topic_score=None, date_bounds=None, entity_agg_size=10):
    tangelo.log("===================================================")
    es = Elasticsearch(elasticsearch_hosts())
    body = entity_histogram_query(email_addrs=email_addrs, query_terms=query_terms, topic_score=topic_score, date_bounds=date_bounds, entity_agg_size=entity_agg_size)

    tangelo.log("get_entity_histogram: query = %s"%body)

    resp = es.search(index=index, doc_type=type,body=body)
    return sorted([dict(d, **{"type":"location"}) for d in resp["aggregations"]["filtered_entity_agg"]["location"]["buckets"]]
                  + [dict(d, **{"type":"organization"}) for d in resp["aggregations"]["filtered_entity_agg"]["organization"]["buckets"]]
                  + [dict(d, **{"type":"person"}) for d in resp["aggregations"]["filtered_entity_agg"]["person"]["buckets"]]
                  + [dict(d, **{"type":"misc"}) for d in resp["aggregations"]["filtered_entity_agg"]["misc"]["buckets"]], key=lambda d:d["doc_count"], reverse=True)

def attachment_histogram(sender_email_addr, start, end, interval="week"):
    tangelo.log('attachment_histogram(%s, %s, %s, %s)' %(sender_email_addr, start, end, interval))
    return {
        "size":0,
        "aggs":{
            "attachments_filter_agg":{"filter" :
                {"bool":{
                    "must":[{"range" : {"datetime" : { "gte": start, "lte": end }}}]
                }
                },

                "aggs" : {
                    "attachments_over_time" : {
                        "date_histogram" : {
                            "field" : "datetime",
                            "interval" : interval,
                            "format" : "yyyy-MM-dd",
                            "min_doc_count" : 0,
                            "extended_bounds":{
                                "min": start,
                                # "max" doesnt really work unless it's set to "now"
                                "max": end
                            }
                        }
                    }
                }
            }

        }
    }


# Get the atachment activity histogram for a specific email address
def attachment_histogram_from_emails(email_addr, date_bounds, interval="week"):
    tangelo.log('attachment_histogram(%s, %s, %s)' %(email_addr, date_bounds, interval))

    # TODO extrac this as an "email_address" generic query
    query  = {
        "filtered": {
            "query": {
                "bool": {
                    "must": [
                        {
                            "match_all": {}
                        }
                    ]
                }
            },
            "filter": {
                "bool": {
                    "must": [
                        {
                            "term": {
                                "addr": email_addr
                            }
                        }
                    ],
                    "should": [
                    ]
                }
            }
        }
    }
    agg = {
        "emailer_attach_agg" : {
            "nested" : {
                "path" : "sender_attachments"
            },
            "aggs" : {
                "sent_attachments_over_time" : {
                    "date_histogram" : {
                        "field" : "sender_attachments.datetime",
                        "interval" : interval,
                        "format" : "yyyy-MM-dd",
                        "min_doc_count" : 0,
                        "extended_bounds":{
                            "min": date_bounds[0],
                            "max": date_bounds[1]
                        }
                    }
                }
            }
        }
    }
    return {"query": query, "aggs":agg, "size":0}



# Returns a sorted map of
def get_daily_activity(index, account_id, type, query_function, **kwargs):
    es = Elasticsearch(elasticsearch_hosts())
    resp = es.search(index=index, doc_type=type, request_cache="false", body=query_function(**kwargs))
    return [_map_activity(index, account_id, sent_rcvd) for sent_rcvd in zip(resp["aggregations"]["sent_agg"]["sent_emails_over_time"]["buckets"],
                                                                             resp["aggregations"]["rcvr_agg"]["rcvd_emails_over_time"]["buckets"])]


# This function uses the date_histogram with the extended_bounds
# Oddly the max part of the extended bounds doesnt seem to work unless the value is set to
# the string "now"...min works fine as 1970 or a number...
# NOTE:  These filters are specific to a user
def actor_histogram(email_addrs, date_bound=None, interval="week"):
    tangelo.log('actor_histogram(%s, %s, %s)' %(email_addrs, date_bound, interval))
    def hist():
        return {
                    "emails_over_time" : {
                        "date_histogram" : {
                            "field" : "datetime",
                            "interval" : interval,
                            "format" : "yyyy-MM-dd",
                            "min_doc_count" : 0,
                            "extended_bounds":{
                                "min": date_bound[0],
                                # "max" doesnt really work unless it's set to "now"
                                "max": date_bound[1]
                            }
                        }
                    }
                }

    return {
        "size":0,
        "aggs":{
            "sent_agg":{
                "filter" : {
                    "bool":{
                        "should": _addrs_filter(email_addrs),
                        "must": _date_filter(date_bound)
                    }
                },
                "aggs" : hist()
            },
            "rcvr_agg":{"filter" : {"bool":{
                "should": _addrs_filter([], tos=email_addrs, ccs=email_addrs, bccs=email_addrs),
                "must": _date_filter(date_bound)
            }},
                "aggs" : hist()
            }
        }
    }

def detect_activity(index, type, query_function, **kwargs):
    es = Elasticsearch(elasticsearch_hosts())
    resp = es.search(index=index, doc_type=type, body=query_function(**kwargs))
    return resp["aggregations"]["filter_agg"]["emails_over_time"]["buckets"]

def get_total_daily_activity(index, type, query_function, **kwargs):
    es = Elasticsearch(elasticsearch_hosts())
    resp = es.search(index=index, doc_type=type, body=query_function(**kwargs))
    return resp["aggregations"]["filter_agg"]["emails_over_time"]["buckets"]

# Returns a sorted map of
def get_email_activity(index, data_set_id, account_id=None, date_bounds=None, interval="week"):
    es = Elasticsearch(elasticsearch_hosts())
    body = actor_histogram([] if not account_id else [account_id], date_bounds, interval)
    tangelo.log("get_email_activity(query body: %s )" % body)

    resp = es.search(index=index, doc_type="emails", request_cache="false", body=body)
    id = data_set_id if not account_id else account_id
    return [_map_activity(index, id, sent_rcvd) for sent_rcvd in zip(resp["aggregations"]["sent_agg"]["emails_over_time"]["buckets"],
                                                                             resp["aggregations"]["rcvr_agg"]["emails_over_time"]["buckets"])]
# Returns a sorted map of
def get_total_attachment_activity(index, account_id, query_function, **kwargs):
    es = Elasticsearch(elasticsearch_hosts())
    body=query_function(**kwargs)
    resp = es.search(index=index, doc_type="attachments", body=body)
    return [_map_attachments(index, account_id, attachments) for attachments in zip(resp["aggregations"]["attachments_filter_agg"]["attachments_over_time"]["buckets"])]

# Returns a sorted map of
def get_emailer_attachment_activity(index, email_address, date_bounds, interval="week"):
    es = Elasticsearch(elasticsearch_hosts())
    body=attachment_histogram_from_emails(email_address, date_bounds, interval)
    resp = es.search(index=index, doc_type="email_address", body=body)
    return [_map_attachments(index, email_address, attachments) for attachments in zip(resp["aggregations"]["emailer_attach_agg"]["sent_attachments_over_time"]["buckets"])]


if __name__ == "__main__":
    print get_datetime_bounds("sample")
    # body = entity_histogram_query(email_addrs=["jeb@jeb.org"], query_terms="", topic_score=None, date_bounds=("1970","now"), entity_agg_size=10)
    # print body
    # resp = es.search(index="sample", doc_type="emails",body=body)
    # res = get_entity_histogram("sample", "emails", email_addrs=[], query_terms="", topic_score=None, date_bounds=("2000","2002"))
    # print {"entities" : [[str(i), entity ["type"], entity ["key"], entity ["doc_count"]] for i,entity in enumerate(res)]}
    #
    # res = get_entity_histogram("sample", "emails", email_addrs=["oviedon@sso.org"], query_terms="", topic_score=None, date_bounds=("2000","2002"))
    # print res
    res = get_emailer_attachment_activity("sample", email_address="jeb@jeb.org", date_bounds=("2000-01-01", "2002-01-01"), interval="week")
    print res
    print "done"
    # activity = get_email_activity("sample", "jeb@jeb.org", actor_histogram, actor_email_addr="jeb@jeb.org", start="2000", end="2002", interval="week")
    # print activity
    # for s in res:
