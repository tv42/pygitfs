from __future__ import with_statement

import errno
import hashlib
import os

from fs import (
    InsecurePathError,
    WalkMixin,
    CrossDeviceRenameError,
    )

from gitfs import commands

class NotifyOnCloseFile(file):
    def __init__(self, *a, **kw):
        self.__callback = kw.pop('callback')
        super(NotifyOnCloseFile, self).__init__(*a, **kw)

    def close(self):
        self.__callback(self)
        super(NotifyOnCloseFile, self).close()

    def __exit__(self, *a, **kw):
        self.__callback(self)
        return super(NotifyOnCloseFile, self).__exit__(*a, **kw)

class IndexFS(WalkMixin):
    """
    Filesystem using a git index file for tracking.

    File contents are stored in a git repository.  As files are not
    (necessarily) reachable from any refs, a ``git gc`` will prune
    them after two weeks. Do not use this filesystem for periods
    longer than this; it is meant for preparing a tree to be
    committed, and that should not take that long.

    Note that files starting with the index filename and a dot are
    used as temporary files.
    """

    def __init__(self, repo, index, path=None):
        self.repo = repo
        self.index = index
        if path is None:
            path = ''
        self.path = path
        self.open_files = {}

    def __repr__(self):
        return '%s(path=%r, index=%r, repo=%r)' % (
            self.__class__.__name__,
            self.path,
            self.index,
            self.repo,
            )

    def name(self):
        """Return last segment of path."""
        return os.path.basename(self.path)

    def join(self, relpath):
        if relpath.startswith(u'/'):
            raise InsecurePathError('path name to join must be relative')
        return self.__class__(
            repo=self.repo,
            index=self.index,
            path=os.path.join(self.path, relpath),
            )

    def child(self, *segments):
        p = self
        for segment in segments:
            if u'/' in segment:
                raise InsecurePathError(
                    'child name contains directory separator')
            # this may be too naive
            if segment == u'..':
                raise InsecurePathError(
                    'child trying to climb out of directory')
            p = p.join(segment)
        return p

    def _get_sha1(self):
        for data in commands.ls_files(
            repo=self.repo,
            index=self.index,
            path=self.path,
            children=False,
            ):
            if data['path'] != self.path:
                continue
            return data['object']

    def open(self, mode='r'):
        path_sha = hashlib.sha1(self.path).hexdigest()
        work = os.path.extsep.join([
                self.index,
                path_sha,
                'work',
                ])

        current_users = self.open_files.get(self.path)
        if current_users is None:

            object = self._get_sha1()
            if object is not None:
                # it exists
                content = commands.cat_file(
                    repo=self.repo,
                    object=object,
                    )
            else:
                content = ''
            tmp = os.path.extsep.join([
                    self.index,
                    path_sha,
                    'tmp',
                    ])
            with file(tmp, 'wb') as f:
                f.write(content)
            os.rename(tmp, work)
            current_users = self.open_files[self.path] = set()

        f = NotifyOnCloseFile(work, mode, callback=self._close_file)
        current_users.add(f)
        return f

    def _close_file(self, f):
        # flush it so we can open it by name and actually see the data
        f.flush()
        current_users = self.open_files[self.path]
        current_users.remove(f)
        if not current_users:
            # last user closed the file, write it to git object
            # storage and update index
            with file(f.name, 'rb') as slurp:
                content = slurp.read()
            os.unlink(f.name)
            object = commands.write_object(
                repo=self.repo,
                content=content,
                )
            commands.update_index(
                repo=self.repo,
                index=self.index,
                files=[
                    dict(
                        # TODO mode, stat the file?
                        object=object,
                        path=self.path,
                        ),
                    ],
                )
            del self.open_files[self.path]

    def __iter__(self):
        last_subdir = None
        for data in commands.ls_files(
            repo=self.repo,
            index=self.index,
            path=self.path,
            children=True,
            ):
            if self.path == '':
                prefix = ''
            else:
                prefix = self.path + '/'
                assert data['path'][:len(prefix)] == prefix
            relative = data['path'][len(prefix):]

            if relative == '.gitfs-placeholder':
                # hide the magic
                continue

            if '/' in relative:
                # it's a subdir, really; combine multiple files into
                # one dir entry
                head = relative.split(os.path.sep, 1)[0]
                if head == last_subdir:
                    # already handled this one
                    continue
                else:
                    last_subdir = head
                    yield self.child(head)
            else:
                yield self.child(relative)

    def parent(self):
        head, tail = os.path.split(self.path)
        return self.__class__(
            repo=self.repo,
            index=self.index,
            path=head,
            )

    def __eq__(self, other):
        if not isinstance(other, IndexFS):
            return NotImplemented
        if (self.repo != other.repo
            or self.index != other.index):
            return False
        if self.path != other.path:
            return False
        return True

    def __ne__(self, other):
        return not self == other

    def __lt__(self, other):
        if not isinstance(other, IndexFS):
            return NotImplemented
        if (self.repo != other.repo
            or self.index != other.index):
            return NotImplemented
        return self.path < other.path

    def __le__(self, other):
        if not isinstance(other, IndexFS):
            return NotImplemented
        return self < other or self == other

    def __gt__(self, other):
        if not isinstance(other, IndexFS):
            return NotImplemented
        if (self.repo != other.repo
            or self.index != other.index):
            return NotImplemented
        return self.path > other.path

    def __ge__(self, other):
        if not isinstance(other, IndexFS):
            return NotImplemented
        return self > other or self == other

    def mkdir(self, may_exist=False, create_parents=False):
        if not may_exist:
            if self.exists():
                raise OSError(errno.EEXIST, os.strerror(errno.EEXIST))

        # path has no children, therefore it is an empty directory
        # and do not exist in the git world; put in a placeholder
        # file
        if not create_parents:
            # make sure parents exist
            if self.parent() != self:
                if not self.parent().exists():
                    raise OSError(
                        errno.ENOENT,
                        os.strerror(errno.ENOENT),
                        )

        empty = commands.write_object(
            repo=self.repo,
            content='',
            )
        commands.update_index(
            repo=self.repo,
            index=self.index,
            files=[
                dict(
                    mode='100644',
                    object=empty,
                    path=self.child('.gitfs-placeholder').path,
                    ),
                ],
            )

    def remove(self):
        commands.update_index(
            repo=self.repo,
            index=self.index,
            files=[
                dict(
                    mode='0',
                    object=40*'0',
                    path=self.path,
                    ),
                ],
            )

    def unlink(self):
        self.remove()

    def isdir(self):
        for data in commands.ls_files(
            repo=self.repo,
            index=self.index,
            path=self.path,
            ):
            return True

        # i have no children, therefore i am not a directory
        return False

    def exists(self):
        if self.path == '':
            # root directory always exists
            return True
        for data in commands.ls_files(
            repo=self.repo,
            index=self.index,
            path=self.path,
            children=False,
            ):
            # doesn't matter if it matches the file itself, or files
            # in a subdirectory; anyway, current path exists
            return True
        return False

    def rmdir(self):
        self.child('.gitfs-placeholder').remove()

    def islink(self):
        if self.path == '':
            # root directory is never a link
            return False
        for data in commands.ls_files(
            repo=self.repo,
            index=self.index,
            path=self.path,
            children=False,
            ):
            if data['path'] == self.path:
                return data['mode'] == '120000'
            else:
                # if current path has children, it can't be a symlink
                assert data['path'].startswith(self.path + '/')
                return False

        # didn't match anything -> don't even exist
        return False

    def rename(self, new_path):
        if not isinstance(new_path, IndexFS):
            raise CrossDeviceRenameError()

        def g():
            for data in commands.ls_files(
                repo=self.repo,
                index=self.index,
                path=self.path,
                children=False,
                ):
                if data['path'] == self.path:
                    data['path'] = new_path.path
                else:
                    prefix = self.path + '/'
                    assert data['path'][:len(prefix)] == prefix
                    data['path'] = new_path.path + '/' + data['path'][len(prefix):]
                yield data

        commands.update_index(
            repo=self.repo,
            index=self.index,
            files=g(),
            )

        self.path = new_path.path
        # TODO don't return self, mutating is good enough
        return self