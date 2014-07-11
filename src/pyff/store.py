import StringIO
from datetime import datetime
from iso8601 import iso8601
from redis import Redis
import time
from pyff.constants import NS, ATTRS
from pyff.utils import root, dumptree, duration2timedelta, totimestamp, parse_xml, hex_digest
from pyff.logs import log


def is_idp(entity):
    return bool(entity.find(".//{%s}IDPSSODescriptor" % NS['md']) is not None)


def is_sp(entity):
    return bool(entity.find(".//{%s}SPSSODescriptor" % NS['md']) is not None)


def entity_attribute_dict(entity):
    d = {}
    entity_id = entity.get('entityID')
    for ea in entity.findall(".//{%s}EntityAttributes" % NS['mdattr']):
        a = ea.find(".//{%s}Attribute" % NS['saml'])
        if a is not None:
            an = a.get('Name', None)
            if a is not None:
                values = [v.text.strip() for v in a.findall(".//{%s}AttributeValue" % NS['saml'])]
                d[an] = values

    d[ATTRS['role']] = []

    if is_idp(entity):
        d[ATTRS['role']].append('idp')
    if is_sp(entity):
        d[ATTRS['role']].append('sp')

    return d


def _now():
    return int(time.time())


class RedisStore(object):
    def __init__(self, version=_now(), default_ttl=3600*24*4, respect_validity=True):
        self.rc = Redis()
        self.default_ttl = default_ttl
        self.respect_validity = respect_validity

    def _expiration(self, relt):
        ts = _now()+self.default_ttl

        if self.respect_validity:
            valid_until = relt.get("validUntil", None)
            if valid_until is not None:
                dt = iso8601.parse_date(valid_until)
                if dt is not None:
                    ts = totimestamp(dt)

            cache_duration = relt.get("cacheDuration", None)
            if cache_duration is not None:
                dt = datetime.now() + duration2timedelta(cache_duration)
                if dt is not None:
                    ts = totimestamp(dt)

        return ts

    def reset(self):
        self.rc.flushdb()

    def periodic(self, stats):
        now = _now()
        log.debug("periodic maintentance...")
        self.rc.zremrangebyscore("entities#members", "-inf", now)
        for c in self.rc.smembers("#collections"):
            self.rc.zremrangebyscore("%s#members", "-inf", now)
            if not self.rc.zcard("%s#members" % c) > 0:
                log.debug("dropping empty collection %s" % c)
                self.rc.srem("#collections", c)
        for an in self.rc.smembers("#attributes"):
            self.rc.zremrangebyscore("%s#values", "-inf", now)
            if not self.rc.zcard("%s#members" % an) > 0:
                log.debug("dropping empty attribute %s" % an)
                self.rc.srem("#attributes", an)

    def update_entity(self, relt, t, tid, ts, p=None):
        if p is None:
            p = self.rc
        p.set("%s#metadata" % tid, dumptree(t))
        if ts is not None:
            p.expireat("%s#metadata" % tid, ts)
        nfo = dict(expires=ts)
        nfo.update(**relt.attrib)
        p.hmset(tid, nfo)
        if ts is not None:
            p.expireat(tid, ts)

    def membership(self, gid, mid, ts, p=None):
        if p is None:
            p = self.rc
        p.zadd("%s#members" % gid, mid, ts)
        #p.zadd("%s#groups", mid, gid, ts)
        p.sadd("#collections", gid)

    def attributes(self):
        return self.rc.smembers("#attributes")

    def attribute(self, an):
        return self.rc.zrangebyscore("%s#values" % an, _now(), "+inf")

    def collections(self):
        return self.rc.smembers("#collections")

    def set(self, key, mapping):
        self.rc.hmset(key, mapping)

    def get(self, key):
        return self.rc.hgetall(key)

    def update(self, t, tid=None, ts=None, merge_strategy=None):
        relt = root(t)
        ne = 0
        if ts is None:
            ts = int(_now()+3600*24*4)    # 4 days is the arbitrary default expiration
        if relt.tag == "{%s}EntityDescriptor" % NS['md']:
            if tid is None:
                tid = relt.get('entityID')
            with self.rc.pipeline() as p:
                self.update_entity(relt, t, tid, ts, p)
                for ea, eav in entity_attribute_dict(relt).iteritems():
                    for v in eav:
                        #log.debug("%s=%s" % (ea, v))
                        self.membership("{%s}%s" % (ea, v), tid, ts, p)
                        p.zadd("%s#values" % ea, v, ts)
                    p.sadd("#attributes", ea)

                for hn in ('sha1', 'sha256', 'md5'):
                    tid_hash = hex_digest(tid, hn)
                    p.set("{%s}%s#alias" % (hn, tid_hash), tid)
                    if ts is not None:
                        p.expireat(tid_hash, ts)
                p.execute()
            ne += 1
        elif relt.tag == "{%s}EntitiesDescriptor" % NS['md']:
            if tid is None:
                tid = relt.get('Name')
            ts = self._expiration(relt, ts)
            with self.rc.pipeline() as p:
                self.update_entity(relt, t, tid, ts, p)
                for e in t.findall(".//{%s}EntityDescriptor" % NS['md']):
                    ne += self.update(e, ts=ts)
                    entity_id = e.get("entityID")
                    self.membership(tid, entity_id, ts, p)
                    self.membership("entities", entity_id, ts, p)
                p.execute()
        else:
            raise ValueError("Bad metadata top-level element: '%s'" % root(t).tag)

        return ne

    def members(self, k):
        if self.rc.exists("%s#members" % k):
            return [self.lookup(entity_id) for entity_id in self.rc.zrangebyscore("%s#members" % k, _now(), "+inf")]
        else:
            return []

    def lookup(self, key):
        if '+' in key:
            hk = hex_digest(key)
            if not self.rc.exists("%s#members" % hk):
                self.rc.zinterstore(hk, ["%s#members" % k for k in key.split('+')], 'min')
                self.rc.expire(hk, 30)  # XXX bad juju - only to keep clients from hammering
            return self.lookup(hk)
        elif self.rc.exists("%s#alias" % key):
            return self.lookup(self.rc.get("%s#alias" % key))
        elif self.rc.exists("%s#metadata" % key):
            return root(parse_xml(StringIO.StringIO(self.rc.get("%s#metadata" % key))))
        else:
            return self.members(key)

    def size(self):
        return self.rc.zcount("entities#members", _now(), "+inf")
