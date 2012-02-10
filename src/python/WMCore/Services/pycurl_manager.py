#!/usr/bin/env python
#-*- coding: ISO-8859-1 -*-
#pylint: disable-msg=R0913,W0702,R0914,R0912,R0201
"""
File: pycurl_manager.py
Author: Valentin Kuznetsov <vkuznet@gmail.com>
Description: a basic wrapper around pycurl library.
The RequestHandler class provides basic APIs to get data
from a single resource or submit mutliple requests to
underlying data-services.
"""

import time
import pycurl
import urllib
from httplib import HTTPException
from WMCore.Wrappers import JsonWrapper as json
try:
    import cStringIO as StringIO
except:
    import StringIO

class ResponseHeader(object):
    """ResponseHeader parses HTTP response header"""
    def __init__(self, response):
        super(ResponseHeader, self).__init__()
        self.header = {}
        self.parse(response)
        self.reason = ''
        self.fromcache = False

    def parse(self, response):
        """Parse response header and assign class member data"""
        for row in response.split('\r'):
            row = row.replace('\n', '')
            if  not row:
                continue
            if  row.find('HTTP') != -1 and \
                row.find('100') == -1: #HTTP/1.1 100 found: real header is later
                res = row.replace('HTTP/1.1', '')
                res = res.replace('HTTP/1.0', '')
                res = res.strip()
                status, reason = res.split(' ', 1)
                self.status = int(status)
                self.reason = reason
                continue
            try:
                key, val = row.split(':', 1)
                self.header[key.strip()] = val.strip()
            except:
                pass

class RequestHandler(object):
    """
    RequestHandler provides APIs to fetch single/multiple
    URL requests based on pycurl library
    """
    def __init__(self, config=None):
        super(RequestHandler, self).__init__()
        if  not config:
            config = {}
        self.nosignal = config.get('nosignal', 1)
        self.timeout = config.get('timeout', 30)
        self.connecttimeout = config.get('connecttimeout', 30)
        self.followlocation = config.get('followlocation', 1)
        self.maxredirs = config.get('maxredirs', 5)

    def set_opts(self, curl, url, params, headers,
                 ckey=None, cert=None, verbose=None, verb='GET', doseq=True):
        """Set options for given curl object"""
        curl.setopt(pycurl.NOSIGNAL, self.nosignal)
        curl.setopt(pycurl.TIMEOUT, self.timeout)
        curl.setopt(pycurl.CONNECTTIMEOUT, self.connecttimeout)
        curl.setopt(pycurl.FOLLOWLOCATION, self.followlocation)
        curl.setopt(pycurl.MAXREDIRS, self.maxredirs)

        if  verb == 'GET':
            encoded_data = urllib.urlencode(params, doseq=doseq)
            url = url + '?' + encoded_data
        elif verb == 'POST':
            curl.setopt(pycurl.POST, 1)
            if params:
                curl.setopt(pycurl.POSTFIELDS, params)
        elif verb == 'DELETE':
            curl.setopt(pycurl.CUSTOMREQUEST, 'DELETE')
        elif verb == 'PUT':
            curl.setopt(pycurl.CUSTOMREQUEST, 'PUT')
            curl.setopt(pycurl.HTTPHEADER, 'Transfer-Encoding: chunked')
            curl.setopt(pycurl.POSTFIELDS, params)
        else:
            raise Exception('Unsupported HTTP method "%s"' % verb)

        curl.setopt(pycurl.URL, url)
        curl.setopt(pycurl.HTTPHEADER, \
                ["%s: %s" % (k, v) for k, v in headers.items()])
        bbuf = StringIO.StringIO()
        hbuf = StringIO.StringIO()
        curl.setopt(pycurl.WRITEFUNCTION, bbuf.write)
        curl.setopt(pycurl.HEADERFUNCTION, hbuf.write)
        curl.setopt(pycurl.SSL_VERIFYPEER, False)
        if  ckey:
            curl.setopt(pycurl.SSLKEY, ckey)
        if  cert:
            curl.setopt(pycurl.SSLCERT, cert)
        if  verbose:
            curl.setopt(pycurl.VERBOSE, 1)
            curl.setopt(pycurl.DEBUGFUNCTION, self.debug)
        return bbuf, hbuf

    def debug(self, debug_type, debug_msg):
        """Debug callback implementation"""
        print "debug(%d): %s" % (debug_type, debug_msg)

    def parse_body(self, data, decode=False):
        """
        Parse body part of URL request (by default use json).
        This method can be overwritten.
        """
        if  decode:
            try:
                res = json.loads(data)
            except ValueError as exc:
                msg = 'Unable to load JSON data\n%s' % data
                raise ValueError(exc)
            return res
        else:
            return data

    def parse_header(self, header):
        """
        Parse response header.
        This method can be overwritten.
        """
        return ResponseHeader(header)

    def request(self, url, params, headers=None, verb='GET',
                verbose=0, ckey=None, cert=None, doseq=True, decode=False):
        """Fetch data for given set of parameters"""
        curl = pycurl.Curl()
        bbuf, hbuf = self.set_opts(curl, url, params, headers,
                ckey, cert, verbose, verb, doseq)
        curl.perform()
        header = self.parse_header(hbuf.getvalue())
        if  header.status == 200:
            data = self.parse_body(bbuf.getvalue(), decode)
        else:
            data = bbuf.getvalue()
            exc = HTTPException()
            setattr(exc, 'req_data', params)
            setattr(exc, 'req_headers', headers)
            setattr(exc, 'url', url)
            setattr(exc, 'result', data)
            setattr(exc, 'status', header.status)
            setattr(exc, 'reason', header.reason)
            setattr(exc, 'headers', header.header)
            raise exc

        bbuf.flush()
        hbuf.flush()
        return header, data

    def getdata(self, url, params, headers=None, verb='GET',
                verbose=0, ckey=None, cert=None, doseq=True):
        """Fetch data for given set of parameters"""
        _, data = self.request(url, params, headers, verb,
                    verbose, ckey, cert, doseq)
        return data

    def getheader(self, url, params, headers=None, verb='GET',
                verbose=0, ckey=None, cert=None, doseq=True):
        """Fetch HTTP header"""
        header, _ = self.request(url, params, headers, verb,
                    verbose, ckey, cert, doseq)
        return header

    def multirequest(self, url, parray, headers=None,
                ckey=None, cert=None, verbose=None):
        """Fetch data for given set of parameters"""
        multi = pycurl.CurlMulti()
        for params in parray:
            curl = pycurl.Curl()
            bbuf, hbuf = \
                self.set_opts(curl, url, params, headers, ckey, cert, verbose)
            multi.add_handle(curl)
            while True:
                ret, num_handles = multi.perform()
                if  ret != pycurl.E_CALL_MULTI_PERFORM:
                    break
            while num_handles:
                ret = multi.select(1.0)
                if  ret == -1:
                    continue
                while 1:
                    ret, num_handles = multi.perform()
                    if  ret != pycurl.E_CALL_MULTI_PERFORM:
                        break
            _numq, response, _err = multi.info_read()
            for _cobj in response:
                data = json.loads(bbuf.getvalue())
                if  isinstance(data, dict):
                    data.update(params)
                    yield data
                if  isinstance(data, list):
                    for item in data:
                        if  isinstance(item, dict):
                            item.update(params)
                            yield item
                        else:
                            err = 'Unsupported data format: data=%s, type=%s'\
                                % (item, type(item))
                            raise Exception(err)
                bbuf.flush()
                hbuf.flush()
