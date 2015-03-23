#!/usr/bin/env python

import logging
import os
from subprocess import (
    check_call, check_output, CalledProcessError, Popen, PIPE)
import sys


def check_output_lines(*popenargs, **kwargs):
    """Like subprocess.check_output(), but yield output lines, lazily."""
    p = Popen(*popenargs, stdout=PIPE, **kwargs)
    for line in p.stdout:
        yield line
    retcode = p.wait()
    if retcode:
        raise CalledProcessError(retcode, p.args)


class Git(object):
    """Wrap various Git commands."""
    def __init__(self, git_dir):
        self.git_dir = git_dir
        self.log = logging.getLogger('Git')

    def args(self, *args):
        return ['git', '--git-dir=' + str(self.git_dir)] + list(args)

    def config_get_one(self, key):
        self.log.debug('config_get_one({})'.format(key))
        try:
            return check_output(self.args(
                'config', '--get', key)).decode('utf-8').rstrip()
        except CalledProcessError:
            return None

    def config_get_all(self, key):
        self.log.debug('config_get_all({})'.format(key))
        try:
            for line in check_output_lines(self.args('config', '--get', key)):
                yield line.decode('utf-8').rstrip()
        except CalledProcessError:
            pass

    def ls_remote(self, remote_or_url):
        self.log.debug('ls_remote({})'.format(remote_or_url))
        for line in check_output_lines(self.args('ls-remote', remote_or_url)):
            self.log.debug('Line: {!r}'.format(line))
            value, refname = line.decode('utf-8').rstrip().split('\t', 1)
            yield (value, refname)

    def fetch(self, remote_or_url, *refspecs):
        self.log.debug('fetch({}, {})'.format(
            remote_or_url, ', '.join(refspecs)))
        check_call(self.args('fetch', remote_or_url, *refspecs))

    def refs(self, pattern):
        self.log.debug('refs({})'.format(pattern))
        args = self.args(
            'for-each-ref', '--format=%(objectname)%00%(refname)', pattern)
        for line in check_output_lines(args):
            self.log.debug('Line: {!r}'.format(line))
            value, refname = line.decode('utf-8').rstrip().split('\0')
            yield (value, refname)


class RefSpec(object):
    @classmethod
    def parse(cls, refspec):
        """Parse a refspec string.

        Parse a string on the form '+refs/heads/*:refs/remotes/foo/*' into the
        equivalent RefSpec instance.
        """
        force = refspec.startswith('+')
        if force:
            refspec = refspec[1:]
        l, r = refspec.split(':')
        if not (l.endswith('*') and r.endswith('*')):
            raise ValueError("Invalid refspec: {}".format(refspec))
        return cls(l[:-1], r[:-1], force)

    def __init__(self, left_prefix, right_prefix, force):
        self.left_prefix = left_prefix
        self.right_prefix = right_prefix
        self.force = force

    def __str__(self):
        return '{}{}*:{}*'.format(
            '+' if self.force else '',
            self.left_prefix,
            self.right_prefix)

    def ltr(self, left_ref):
        """Map the given ref from the left to the right side of this refspec.

        Return None iff left_ref does not match the left side.
        """
        if left_ref.startswith(self.left_prefix):
            return self.right_prefix + left_ref[len(self.left_prefix):]
        return None

    def rtl(self, right_ref):
        """Map the given ref from the right to the left side of this refspec.

        Return None iff right_ref does not match the right side.
        """
        if right_ref.startswith(self.right_prefix):
            return self.left_prefix + right_ref[len(self.right_prefix):]
        return None

    def with_left(self, left_prefix, force=None):
        """Return a new RefSpec with the given left_prefix."""
        if force is None:
            force = self.force
        return self.__class__(left_prefix, self.right_prefix, force)

    def with_right(self, right_prefix, force=None):
        """Return a new RefSpec with the given right_prefix."""
        if force is None:
            force = self.force
        return self.__class__(self.left_prefix, right_prefix, force)


class DirSpec(object):
    @classmethod
    def parse(cls, dirspec):
        """Parse a dirspec string.

        Parse a string on the form 'foo/bar:blarg' into the equivalent DirSpec
        instance.
        """
        l, r = dirspec.split(':')
        if l.startswith('/') or r.startswith('/'):
            raise ValueError("Invalid dirspec: {}".format(dirspec))
        return cls(l.rstrip('/'), r.rstrip('/'))

    def __init__(self, left_dir, right_dir):
        self.left_dir = left_dir
        self.right_dir = right_dir

    def __str__(self):
        return '{}:{}'.format(self.left_dir, self.right_dir)

    def ltr(self, tree_sha1):
        """Map the given tree from the left to the right side of this dirspec.

        Return the sha1 of result tree.
        """
        if self.left_dir:
            raise NotImplementedError("Don't know how to extract left_dir")
        if self.right_dir:
            raise NotImplementedError("Don't know how to insert right_dir")

        return tree_sha1


class RemoteSubdirHelper(object):

    # Where to store remote-tracking refs before mapping through dirspec.
    UnmappedRefPrefix = 'refs/remote-subdir/{0}/'

    # Where to store remote-tracking refs after mapping through dirspec
    MappedRefPrefix = 'refs/remotes/{0}/'

    def __init__(self, git_dir, remote_name):
        self.log = logging.getLogger('RemoteSubdirHelper')
        self.log.debug(
            'Constructing with (git_dir={!r}, remote_name={!r})'.format(
                git_dir, remote_name))

        self.git = Git(git_dir)
        self.remote = remote_name
        self.url = self.git.config_get_one('remote.{}.url'.format(self.remote))
        self.dirspec = DirSpec.parse(self.git.config_get_one(
            'remote.{}.dirspec'.format(self.remote)))  # TODO: More dirspecs?
        self.refspecs = [RefSpec.parse(s) for s in self.git.config_get_all(
            'remote.{}.fetch'.format(self.remote))]

        if not (self.url and self.dirspec and self.refspecs):
            raise ValueError(
                'Missing one or more of remote.{}.url/dirspec/fetch'.format(
                    self.remote))
        if self.url.startswith('subdir:'):  # TODO: Nested subdir-remotes?
            raise ValueError(
                'remote.{0}.url cannot start with "subdir:". Instead '
                'configure remote.{0}.vcs = subdir'.format(self.remote))

        self.unmapped_ref_prefix = self.UnmappedRefPrefix.format(self.remote)
        self.mapped_ref_prefix = self.MappedRefPrefix.format(self.remote)

        # Split each refspec remote_ref_prefix*:mapped_ref_prefix* in two:
        #  - remote_ref_prefix*:unmapped_ref_prefix*
        #  - unmapped_ref_prefix*:mapped_ref_prefix*
        self.fetchspecs, self.mapspecs = zip(*[
            self.split_refspec(refspec) for refspec in self.refspecs])

        self.log.debug("Will fetch from {}".format(self.url))
        for phase1, phase2 in zip(self.fetchspecs, self.mapspecs):
            self.log.debug("Will fetch refs {} -> {}".format(phase1, phase2))
        self.log.debug("Will map dirs {}".format(self.dirspec))

    def split_refspec(self, refspec):
        remove = self.mapped_ref_prefix
        insert = self.unmapped_ref_prefix
        if not refspec.right_prefix.startswith(remove):
            raise ValueError((
                'Cannot work with refspec {}. The right side does not start '
                'with {}!').format(refspec, remove))
        updated = insert + refspec.right_prefix[len(remove):]
        return (
            refspec.with_right(updated, force=True),
            refspec.with_left(updated))

    def do_capabilities(self, f):
        f.write('*fetch\n')
#        f.write('check-connectivity\n')
#        f.write('option\n')
        f.write('\n')

#    def do_option(self, f, name, value):
#        supported = {('followtags', 'true')}
#        f.write('ok\n' if (name, value) in supported else 'unsupported\n')

    def do_list(self, f):
        # Here, we'd like run git ls-remote against remote repo, and for each
        # (unmapped-value, remote-ref) returned, we'd like to do the following:
        #  - If we've already seen the unmapped-value, then map it through
        #    our dirspec to get the mapped-value to return to our caller as
        #    the first argument.
        #  - Otherwise, we don't (yet) have a mapping for unmapped-value, so
        #    we instead return a '?' as the first argument.
        #  - The remote-ref is returned to our caller as the second
        #    argument.
        #  - If we have seen remote-ref before, and its unmapped-value is equal
        #    to refs/remote-subdir/remote-name/remote-ref, then we should
        #    append 'unchanged' as a last argument.
        #  - Return the arguments (separated by a space) to our caller.
        #
        # HOWEVER, it seems that transport-helper.c currently does not handle
        # unknown ref values correctly: When returning '?' as the first
        # argument, transport-helper will follow up with a fetch request like:
        #   fetch 0000000000000000000000000000000000000000 refs/heads/foo
        # This doesn't really cause any problems for us (we can happily ignore
        # the null sha1 and simply fetch the the real value of the ref along
        # with the required objects from the remote. But we have no way of
        # communicating the _real_ value of the ref back to transport-helper.c.
        # Instead, the fetch machinery move on to validating the fetched data,
        # which fails hard when it tries to look up the incorrect null sha1.
        #
        # SO INSTEAD, we're forced to never return '?' from a list command, but
        # must instead perform the entire fetch _now_, so that the correct sha1
        # can be returned for each ref.
        self.git.fetch(self.url, *[str(r) for r in self.fetchspecs])
        for value, ref in self.git.refs(self.unmapped_ref_prefix):
            f.write('{} {}\n'.format(value, self.fetchspecs[0].rtl(ref)))
        f.write('\n')

    def do_fetch(self, f, sha1, name):
        raise RuntimeError(
            "Should not get here, as fetch was already done in do_list()...")
#        out_f.write('\n')

    def process_commands(self, in_f, out_f):
        while True:
            cmdline = in_f.readline()
            self.log.debug('---')
            self.log.debug('Command line: {}'.format(cmdline.rstrip()))
            if cmdline in ['', '\n']:  # EOF or end of command stream
                break
            args = cmdline.rstrip().split()
            getattr(self, 'do_' + args[0])(out_f, *args[1:])
            out_f.flush()


def main(remote_or_url, url=None):
    logging.basicConfig(stream=sys.stderr, level='DEBUG')
    logging.debug('{} run from {}'.format(sys.argv[0], os.getcwd()))

    # We only accept a proper git remote name as the first argument, as we
    # need to look up some configured settings that cannot be passed on the
    # command line. RemoteSubdirHelper will raise ValueError when
    # remote_or_url does not name a configure git remote.
    helper = RemoteSubdirHelper(os.environ['GIT_DIR'], remote_or_url)
    helper.process_commands(sys.stdin, sys.stdout)


if __name__ == '__main__':
    main(*sys.argv[1:])
