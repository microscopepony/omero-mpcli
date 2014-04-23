#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Copyright (C) 2014 University of Dundee & Open Microscopy Environment.
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import getpass
import logging
import numpy
import os

import omero
import omero.gateway
import portalocker

import pychrm
from pychrm.FeatureSet import Signatures
from pychrm.PyImageMatrix import PyImageMatrix


log = logging.getLogger('PychrmCalculate')
log.setLevel(logging.DEBUG)


class CalculationException(Exception):

    def __init__(self, msg):
        super(CalculationException, self).__init__(msg)


class Calculator(object):

    def __init__(self, host=None, port=None, user=None, password=None,
                 calculate=None, groupid=-1):
        if not host:
            host = raw_input('Host: ')
        if not port:
            port = 4064
        if not user:
            user = raw_input('User: ')
        if not password:
            password = getpass.getpass()

        self.client = omero.client(host, port)
        self.session = self.client.createSession(user, password)
        self.client.enableKeepAlive(60)
        self.conn = omero.gateway.BlitzGateway(client_obj=self.client)
        self.conn.SERVICE_OPTS.setOmeroGroup(groupid)
        self.detach = True

        self.calculate = calculate

    def close(self):
        if self.detach:
            try:
                self.session.detachOnDestroy()
            except Exception as e:
                print e
        self.client.closeSession()

    def imageGenerator(self, objs):
        for o in objs:
            log.info(o)
            if isinstance(o, omero.gateway._ImageWrapper):
                yield o
            else:
                children = o.listChildren()
                for im in self.imageGenerator(children):
                    yield im

    def genObjects(self, typeids):
        for (t, i) in typeids:
            o = self.conn.getObject(t, i)
            if not o:
                raise CalculationException(
                    'Unable to get object: %s %d' % (t, i))
            yield o

    def genComputationList(self, typeids):
        """
        typeids: A list of (TypeName, Id) pairs where TypeName is Image,
        Dataset or Project
        """
        def planeParamsGen(im):
            for c in xrange(im.getSizeC()):
                for z in xrange(im.getSizeZ()):
                    for t in xrange(im.getSizeT()):
                        yield (im.id, c, z, t, im.getSizeX(), im.getSizeY())

        objGen = self.genObjects(typeids)
        for im in self.imageGenerator(objGen):
            for params in planeParamsGen(im):
                yield params

    def setCalculate(func):
        """
        func is a function that calculates features given a single image plane.
        Note this takes in an image id instead of an image object to support
        batch jobs where a list of parameter sets can be provided.

        func: Function of the form r = func(conn, iid, c, z, t)
          conn: BlitzGateway object
          iid: Image ID
          c, z, t: Index of the C/Z/T plance
          r: A dict with fields names ([string]), values ([double]), and
            version (string)
        """
        self.calculate = func


def extractFeaturesPychrmSmall(conn, iid, c, z, t):
    """
    Calculate features for a single image plane. Note this takes in an
    image id instead of an image object to support batch jobs where a list
    of parameter sets can be provided.
    """
    im = self.conn.getObject('Image', iid)
    if not im:
        raise CalculationException('Image id not found: %d' % iid)

    # Calculate features for a single plane (c/z/t)
    pychrm_matrix = PyImageMatrix()
    pychrm_matrix.allocate(im.getSizeX(), im.getSizeY())
    numpy_matrix = pychrm_matrix.as_ndarray()

    numpy_matrix[:] = im.getPrimaryPixels().getPlane(
        theZ=z, theC=c, theT=t)
    feature_plan = pychrm.StdFeatureComputationPlans.getFeatureSet()
    options = ""  # Wnd-charm options
    fts = Signatures.NewFromFeatureComputationPlan(
        pychrm_matrix, feature_plan, options)

    ft = {
        'names': fts.names,
        'values': ft.values,
        'version': fts.version
    }
    return ft


def meanIntensity(conn, iid, c, z, t):
    """
    Calculate features for a single image plane. Note this takes in an
    image id instead of an image object to support batch jobs where a list
    of parameter sets can be provided.
    """
    im = conn.getObject('Image', iid)
    if not im:
        raise CalculationException('Image id not found: %d' % iid)

    # Calculate features for a single plane (c/z/t)
    m = im.getPrimaryPixels().getPlane(theZ=z, theC=c, theT=t)
    ft = {
        'names': ['min', 'max', 'mean'],
        'values': [m.min(), m.max(), m.mean()],
        'version': '0'
    }
    return ft


class FeatureFile(object):
    """
    A context manager to handle multiple concurrent feature calculations, and
    saving features.

    On entering the context manager an exclusive lockfile named after the image
    parameters is created.
    Features can be written to this file.
    On exit the lockfile is renamed to the final output filename.
    """

    def __init__(self, iid, z, c, t):
        self.dir = 'SmallFeatureSet'
        self.filename = 'image%08d-c%d-z%d-t%d' % (iid, c, z, t)
        self.npy = os.path.join(self.dir, self.filename + '.npy')
        self.saved = False

    def __enter__(self):
        """
        Succeeds if an exclusive lock was obtained and the final output file
        does not exist or is empty, otherwise raises an exception.
        """
        lock = os.path.join(self.dir, self.filename + '.tmp')
        log.debug('Locking: %s', lock)
        self.fh = open(lock, 'w+')
        try:
            portalocker.lock(self.fh, portalocker.LOCK_EX)
            # Check whether the final output file has already been created
            try:
                log.debug('Checking: %s', self.npy)
                # Using with open seems to trigger an ipython bug
                # with open(self.npy, 'r') as f:
                f = open(self.npy, 'r')
                try:
                    f.seek(0, 2)
                    if f.tell() > 0:
                        raise CalculationException(
                            'Feature file already exists: %s' % self.npy)
                finally:
                    f.close()
            except IOError:
                # Assume this is file not found
                pass
            except:
                os.unlink(self.fh.name)
                raise
        except:
            self.fh.close()
            raise
        return self

    def save(self, a):
        log.debug('Saving: %s', self.fh.name)
        numpy.save(self.fh, a)
        self.saved = True

    def __exit__(self, type, value, traceback):
        if self.saved:
            log.debug('Renaming: %s->%s', self.fh.name, self.npy)
            os.rename(self.fh.name, self.npy)
        else:
            log.debug('Empty, deleting: %s', self.fh.name)
            os.unlink(self.fh.name)
        self.fh.close()


def example():
    for iid in range(10):
        try:
            with FeatureFile(iid, 0, 0, 0) as ff:
                a = numpy.array([0, 1, 2])
                ff.save(a)
        except (portalocker.LockException, CalculationException) as e:
            log.error(e)
