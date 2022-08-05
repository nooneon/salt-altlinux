"""
Support for APT (Advanced Packaging Tool)/RPM for ALT Linux

.. important::
    If you feel that Salt should be using this module to manage packages on a
    minion, and it is using a different module (or gives an error similar to
    *'pkg.install' is not available*), see :ref:`here
    <module-provider-override>`.

    For repository management, the ``apt-repo`` package must be installed.

    Because of APT/RPM package management system in ALT Linux this module 
    was combined from two package management salt modules :
      - aptpkg.py
      - yumpkg.py
"""

import copy
import datetime
import fnmatch
import logging
import os
import pathlib
import re
import shutil
import tempfile
import time

from distutils.version import LooseVersion as _LooseVersion

import salt.config
import salt.syspaths
import salt.utils.args
import salt.utils.data
import salt.utils.environment
import salt.utils.files
import salt.utils.functools
import salt.utils.itertools
import salt.utils.json
import salt.utils.path
import salt.utils.pkg
import salt.utils.pkg.deb
import salt.utils.stringutils
import salt.utils.systemd
import salt.utils.versions
import salt.utils.yaml

from salt.exceptions import (
    CommandExecutionError,
    CommandNotFoundError,
    MinionError,
    SaltInvocationError,
)
from salt.modules.cmdmod import _parse_env

log = logging.getLogger(__name__)

PKG_ARCH_SEPARATOR = "."

APT_LISTS_PATH = "/var/lib/apt/lists"

DPKG_ENV_VARS = {
    "APT_LISTBUGS_FRONTEND": "none",
    "APT_LISTCHANGES_FRONTEND": "none",
    "DEBIAN_FRONTEND": "noninteractive",
    "UCF_FORCE_CONFFOLD": "1",
}

# Define the module's virtual name
__virtualname__ = "pkg"


def __virtual__():
    """
    Confirm this module is on a ALT Linux based system
    """
    if __grains__.get("os").lower() == "alt":
        return __virtualname__
    return False, "The pkg module could not be loaded: unsupported OS family"


def __init__(opts):
    """
    For APT-GET systems, set up
    a few env variables to keep apt happy and
    non-interactive.
    """
    if __virtual__() == __virtualname__:
        # Export these puppies so they persist
        os.environ.update(DPKG_ENV_VARS)


class SourceEntry:  # pylint: disable=function-redefined
    def __init__(self, line, file=None):
        self.invalid = False
        self.comps = []
        self.disabled = False
        self.comment = ""
        self.dist = ""
        self.type = ""
        self.uri = ""
        self.line = line
        self.architectures = []
        self.file = file
        if not self.file:
            self.file = str(pathlib.Path(os.sep, "etc", "apt", "sources.list"))
        self._parse_sources(line)

    def repo_line(self):
        """
        Return the repo line for the sources file
        """
        repo_line = []
        if self.invalid:
            return self.line
        if self.disabled:
            repo_line.append("#")

        repo_line.append(self.type)
        if self.architectures:
            repo_line.append("[arch={}]".format(" ".join(self.architectures)))

        repo_line = repo_line + [self.uri, self.dist, " ".join(self.comps)]
        if self.comment:
            repo_line.append("#{}".format(self.comment))
        return " ".join(repo_line) + "\n"

    def _parse_sources(self, line):
        """
        Parse lines from sources files
        """
        self.disabled = False
        repo_line = self.line.strip().split()
        if not repo_line:
            self.invalid = True
            return False
        if repo_line[0].startswith("#"):
            repo_line.pop(0)
            self.disabled = True
        if repo_line[0] not in ["deb", "deb-src", "rpm", "rpm-src"]:
            self.invalid = True
            return False
        if repo_line[1].startswith("["):
            opts = re.search(r"\[.*\]", self.line).group(0).strip("[]")
            repo_line = [x for x in (line.strip("[]") for line in repo_line) if x]
            for opt in opts.split():
                if opt.startswith("arch"):
                    self.architectures.extend(opt.split("=", 1)[1].split(","))
                try:
                    repo_line.pop(repo_line.index(opt))
                except ValueError:
                    repo_line.pop(repo_line.index("[" + opt + "]"))
        self.type = repo_line[0]
        self.uri = repo_line[1]
        self.dist = repo_line[2]
        self.comps = repo_line[3:]

class SourcesList:  # pylint: disable=function-redefined
    def __init__(self):
        self.list = []
        self.files = [
            pathlib.Path(os.sep, "etc", "apt", "sources.list"),
            pathlib.Path(os.sep, "etc", "apt", "sources.list.d"),
        ]
        for file in self.files:
            if file.is_dir():
                for fp in file.glob("**/*.list"):
                    self.add_file(file=fp)
            else:
                self.add_file(file)

    def __iter__(self):
        yield from self.list

    def add_file(self, file):
        """
        Add the lines of a file to self.list
        """
        if file.is_file():
            with salt.utils.files.fopen(file) as source:
                for line in source:
                    self.list.append(SourceEntry(line, file=str(file)))
        else:
            log.debug("The apt sources file %s does not exist", file)

    def add(self, type, uri, dist, orig_comps, architectures):
        repo_line = [
            type,
            " [arch={}] ".format(" ".join(architectures)) if architectures else "",
            uri,
            dist,
            " ".join(orig_comps),
        ]
        return SourceEntry(" ".join(repo_line))

    def remove(self, source):
        """
        remove a source from the list of sources
        """
        self.list.remove(source)

    def save(self):
        """
        write all of the sources from the list of sources
        to the file.
        """
        filemap = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            for source in self.list:
                fname = pathlib.Path(tmpdir, pathlib.Path(source.file).name)
                with salt.utils.files.fopen(fname, "a") as fp:
                    fp.write(source.repo_line())
                if source.file not in filemap:
                    filemap[source.file] = {"tmp": fname}

            for fp in filemap:
                shutil.copy(filemap[fp]["tmp"], fp)
                #os.remove(filemap[fp]["tmp"]) ### not working - could not found temp file


def _call_apt(args, scope=True, **kwargs):
    """
    Call apt* utilities.
    """
    cmd = []
    if (
        scope
        and salt.utils.systemd.has_scope(__context__)
        and __salt__["config.get"]("systemd.scope", True)
    ):
        cmd.extend(["systemd-run", "--scope", "--description", '"{}"'.format(__name__)])
    cmd.extend(args)

    params = {
        "output_loglevel": "trace",
        "python_shell": False,
        "env": salt.utils.environment.get_module_environment(globals()),
    }
    params.update(kwargs)
    log.debug(cmd)
    log.debug(params)
        
    cmd_ret = __salt__["cmd.run_all"](cmd, **params)
    count = 0
    while "Could not get lock" in cmd_ret.get("stderr", "") and count < 10:
        count += 1
        log.warning("Waiting for dpkg lock release: retrying... %s/100", count)
        time.sleep(2 ** count)
        cmd_ret = __salt__["cmd.run_all"](cmd, **params)
    return cmd_ret


def latest_version(*names, **kwargs):
    """
    Return the latest version of the named package available for upgrade or
    installation. If more than one package name is specified, a dict of
    name/version pairs is returned.

    If the latest version of a given package is already installed, an empty
    string will be returned for that package.

    A specific repo can be requested using the ``fromrepo`` keyword argument.

    cache_valid_time

        .. versionadded:: 2016.11.0

        Skip refreshing the package database if refresh has already occurred within
        <value> seconds

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.latest_version <package name>
        salt '*' pkg.latest_version <package name> fromrepo=unstable
        salt '*' pkg.latest_version <package1> <package2> <package3> ...
    """
    refresh = salt.utils.data.is_true(kwargs.pop("refresh", True))
    show_installed = salt.utils.data.is_true(kwargs.pop("show_installed", False))
    if "repo" in kwargs:
        raise SaltInvocationError(
            "The 'repo' argument is invalid, use 'fromrepo' instead"
        )
    fromrepo = kwargs.pop("fromrepo", None)
    cache_valid_time = kwargs.pop("cache_valid_time", 0)

    if not names:
        return ""
    ret = {}
    # Initialize the dict with empty strings
    for name in names:
        ret[name] = ""
    pkgs = list_pkgs(versions_as_list=True)
    repo = ["-o", "APT::Default-Release={}".format(fromrepo)] if fromrepo else None

    # Refresh before looking for the latest version available
    if refresh:
        refresh_db(cache_valid_time)

    for name in names:
        cmd = ["apt-cache", "-q", "policy", name]
        if repo is not None:
            cmd.extend(repo)
        out = _call_apt(cmd, scope=False)

        candidate = ""
        for line in salt.utils.itertools.split(out["stdout"], "\n"):
            if "Candidate" in line:
                comps = line.split()
                if len(comps) >= 2:
                    candidate = comps[-1]
                    if candidate.lower() == "(none)":
                        candidate = ""
                break
        

        # cut right part after semicolon in versions like
        # 4:8.2.5019-alt1:p10+300891.100.2.1@1654002425
        # to normalize version
        semicolon_pos = candidate.rfind(":")
        if semicolon_pos != -1 and semicolon_pos > 2:
            candidate = candidate[0:candidate.rfind(":",1)]

        installed = pkgs.get(name, [])
        if not installed:
            ret[name] = candidate
        elif installed and show_installed:
            ret[name] = candidate
        elif candidate:
            # If there are no installed versions that are greater than or equal
            # to the install candidate, then the candidate is an upgrade, so
            # add it to the return dict
            if not any(
                salt.utils.versions.compare(
                    ver1=x, oper=">=", ver2=candidate, cmp_func=version_cmp
                )
                for x in installed
            ):
                ret[name] = candidate

    # Return a string if only one package name passed
    if len(names) == 1:
        return ret[names[0]]
    return ret


# available_version is being deprecated
available_version = salt.utils.functools.alias_function(
    latest_version, "available_version"
)

def version(*names, **kwargs):
    """
    Returns a string representing the package version or an empty string if not
    installed. If more than one package name is specified, a dict of
    name/version pairs is returned.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.version <package name>
        salt '*' pkg.version <package1> <package2> <package3> ...
    """
    return __salt__["pkg_resource.version"](*names, **kwargs)

def refresh_db(cache_valid_time=0, failhard=False, **kwargs):
    """
    Updates the APT database to latest packages based upon repositories

    Returns a dict, with the keys being package databases and the values being
    the result of the update attempt. Values can be one of the following:

    - ``True``: Database updated successfully
    - ``False``: Problem updating database
    - ``None``: Database already up-to-date

    cache_valid_time

        .. versionadded:: 2016.11.0

        Skip refreshing the package database if refresh has already occurred within
        <value> seconds

    failhard

        If False, return results of Err lines as ``False`` for the package database that
        encountered the error.
        If True, raise an error with a list of the package databases that encountered
        errors.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.refresh_db
    """
    # Remove rtag file to keep multiple refreshes from happening in pkg states
    salt.utils.pkg.clear_rtag(__opts__)
    failhard = salt.utils.data.is_true(failhard)
    ret = {}
    error_repos = list()

    if cache_valid_time:
        try:
            latest_update = os.stat(APT_LISTS_PATH).st_mtime
            now = time.time()
            log.debug(
                "now: %s, last update time: %s, expire after: %s seconds",
                now,
                latest_update,
                cache_valid_time,
            )
            if latest_update + cache_valid_time > now:
                return ret
        except TypeError as exp:
            log.warning(
                "expected integer for cache_valid_time parameter, failed with: %s", exp
            )
        except OSError as exp:
            log.warning("could not stat cache directory due to: %s", exp)

    call = _call_apt(["apt-get", "-q", "update"], scope=False)
    if call["retcode"] != 0:
        comment = ""
        if "stderr" in call:
            comment += call["stderr"]

        raise CommandExecutionError(comment)
    else:
        out = call["stdout"]

    for line in out.splitlines():
        cols = line.split()
        if not cols:
            continue
        ident = " ".join(cols[1:])
        if "Get" in cols[0]:
            # Strip filesize from end of line
            ident = re.sub(r" \[.+B\]$", "", ident)
            ret[ident] = True
        elif "Ign" in cols[0]:
            ret[ident] = False
        elif "Hit" in cols[0]:
            ret[ident] = None
        elif "Err" in cols[0]:
            ret[ident] = False
            error_repos.append(ident)

    if failhard and error_repos:
        raise CommandExecutionError(
            "Error getting repos: {}".format(", ".join(error_repos))
        )

    return ret

# update is an alias to refresh_db
update = salt.utils.functools.alias_function(
    refresh_db, "update"
)

def install(
    name=None,
    refresh=False,
    fromrepo=None,
    skip_verify=False,
    debconf=None,
    pkgs=None,
    sources=None,
    reinstall=False,
    downloadonly=False,
    ignore_epoch=False,
    **kwargs
):
    """
    .. versionchanged:: 2015.8.12,2016.3.3,2016.11.0
        On minions running systemd>=205, `systemd-run(1)`_ is now used to
        isolate commands which modify installed packages from the
        ``salt-minion`` daemon's control group. This is done to keep systemd
        from killing any apt-get/dpkg commands spawned by Salt when the
        ``salt-minion`` service is restarted. (see ``KillMode`` in the
        `systemd.kill(5)`_ manpage for more information). If desired, usage of
        `systemd-run(1)`_ can be suppressed by setting a :mod:`config option
        <salt.modules.config.get>` called ``systemd.scope``, with a value of
        ``False`` (no quotes).

    .. _`systemd-run(1)`: https://www.freedesktop.org/software/systemd/man/systemd-run.html
    .. _`systemd.kill(5)`: https://www.freedesktop.org/software/systemd/man/systemd.kill.html

    Install the passed package, add refresh=True to update the dpkg database.

    name
        The name of the package to be installed. Note that this parameter is
        ignored if either "pkgs" or "sources" is passed. Additionally, please
        note that this option can only be used to install packages from a
        software repository. To install a package file manually, use the
        "sources" option.

        32-bit packages can be installed on 64-bit systems by appending the
        architecture designation (``:i386``, etc.) to the end of the package
        name.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.install <package name>

    refresh
        Whether or not to refresh the package database before installing.

    cache_valid_time

        .. versionadded:: 2016.11.0

        Skip refreshing the package database if refresh has already occurred within
        <value> seconds

    fromrepo
        Specify a package repository to install from
        (e.g., ``apt-get -t unstable install somepackage``)

    skip_verify
        Skip the GPG verification check (e.g., ``--allow-unauthenticated``, or
        ``--force-bad-verify`` for install from package file).

    debconf
        Provide the path to a debconf answers file, processed before
        installation.

    version
        Install a specific version of the package, e.g. 0.7.4-alt2.noarch. Ignored
        if "pkgs" or "sources" is passed.

        .. versionchanged:: 2018.3.0
            version can now contain comparison operators (e.g. ``>1.2.3``,
            ``<=2.0``, etc.)

    reinstall : False
        Specifying reinstall=True will use ``apt-get install --reinstall``
        rather than simply ``apt-get install`` for requested packages that are
        already installed.

        If a version is specified with the requested package, then ``apt-get
        install --reinstall`` will only be used if the installed version
        matches the requested version.

        .. versionadded:: 2015.8.0

    ignore_epoch : False
        Only used when the version of a package is specified using a comparison
        operator (e.g. ``>4.1``). If set to ``True``, then the epoch will be
        ignored when comparing the currently-installed version to the desired
        version.

        .. versionadded:: 2018.3.0

    Multiple Package Installation Options:

    pkgs
        A list of packages to install from a software repository. Must be
        passed as a python list.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.install pkgs='["foo", "bar"]'
            salt '*' pkg.install pkgs='["foo", {"bar": "0.7.4-alt2.noarch"}]'

    sources
        A list of RPM packages to install. Must be passed as a list of dicts,
        with the keys being package names, and the values being the source URI
        or local path to the package.  Dependencies are automatically resolved
        and marked as auto-installed.

        32-bit packages can be installed on 64-bit systems by appending the
        architecture designation (``:i386``, etc.) to the end of the package
        name.

        .. versionchanged:: 2014.7.0

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.install sources='[{"foo": "salt://foo.rpm"},{"bar": "salt://bar.rpm"}]'

    force_yes
        Passes ``--force-yes`` to the apt-get command.  Don't use this unless
        you know what you're doing.

        .. versionadded:: 0.17.4

    install_recommends
        Whether to install the packages marked as recommended.  Default is True.

        .. versionadded:: 2015.5.0

    only_upgrade
        Only upgrade the packages, if they are already installed. Default is False.

        .. versionadded:: 2015.5.0

    force_conf_new
        Always install the new version of any configuration files.

        .. versionadded:: 2015.8.0

    Returns a dict containing the new package names and versions::

        {'<package>': {'old': '<old-version>',
                       'new': '<new-version>'}}
    """
    _refresh_db = False
    if salt.utils.data.is_true(refresh):
        _refresh_db = True
        if "version" in kwargs and kwargs["version"]:
            _refresh_db = False
            _latest_version = latest_version(name, refresh=False, show_installed=True)
            _version = kwargs.get("version")
            # If the versions don't match, refresh is True, otherwise no need
            # to refresh
            if not _latest_version == _version:
                _refresh_db = True

        if pkgs:
            _refresh_db = False
            for pkg in pkgs:
                if isinstance(pkg, dict):
                    _name = next(iter(pkg.keys()))
                    _latest_version = latest_version(
                        _name, refresh=False, show_installed=True
                    )
                    _version = pkg[_name]
                    # If the versions don't match, refresh is True, otherwise
                    # no need to refresh
                    if not _latest_version == _version:
                        _refresh_db = True
                else:
                    # No version specified, so refresh should be True
                    _refresh_db = True

    if debconf:
        __salt__["debconf.set_file"](debconf)

    try:
        pkg_params, pkg_type = __salt__["pkg_resource.parse_targets"](
            name, pkgs, sources, **kwargs
        )
    except MinionError as exc:
        raise CommandExecutionError(exc)

    # Support old "repo" argument
    repo = kwargs.get("repo", "")
    if not fromrepo and repo:
        fromrepo = repo

    if not pkg_params:
        return {}

    cmd_prefix = []

    old = list_pkgs()
    targets = []
    downgrade = []
    to_reinstall = {}
    errors = []
    if pkg_type == "repository":
        pkg_params_items = list(pkg_params.items())
        has_comparison = [
            x
            for x, y in pkg_params_items
            if y is not None and (y.startswith("<") or y.startswith(">"))
        ]
        _available = (
            list_repo_pkgs(*has_comparison, byrepo=False, **kwargs)
            if has_comparison
            else {}
        )
        # Build command prefix
        cmd_prefix.extend(["apt-get", "-q", "-y"])
        if kwargs.get("force_yes", False):
            cmd_prefix.append("--force-yes")
        if "force_conf_new" in kwargs and kwargs["force_conf_new"]:
            cmd_prefix.extend(["-o", "DPkg::Options::=--force-confnew"])
        else:
            cmd_prefix.extend(["-o", "DPkg::Options::=--force-confold"])
            cmd_prefix += ["-o", "DPkg::Options::=--force-confdef"]
        if "install_recommends" in kwargs:
            if not kwargs["install_recommends"]:
                cmd_prefix.append("--no-install-recommends")
            else:
                cmd_prefix.append("--install-recommends")
        if "only_upgrade" in kwargs and kwargs["only_upgrade"]:
            cmd_prefix.append("--only-upgrade")
        if skip_verify:
            cmd_prefix.append("--allow-unauthenticated")
        if fromrepo:
            cmd_prefix.extend(["-t", fromrepo])
        cmd_prefix.append("install")
    else:
        pkg_params_items = []
        for pkg_source in pkg_params:
            if "lowpkg.bin_pkg_info" in __salt__:
                deb_info = __salt__["lowpkg.bin_pkg_info"](pkg_source)
            else:
                deb_info = None
            if deb_info is None:
                log.error(
                    "pkg.install: Unable to get deb information for %s. "
                    "Version comparisons will be unavailable.",
                    pkg_source,
                )
                pkg_params_items.append([pkg_source])
            else:
                pkg_params_items.append(
                    [deb_info["name"], pkg_source, deb_info["version"]]
                )
        # Build command prefix
        if "force_conf_new" in kwargs and kwargs["force_conf_new"]:
            cmd_prefix.extend(["dpkg", "-i", "--force-confnew"])
        else:
            cmd_prefix.extend(["dpkg", "-i", "--force-confold"])
        if skip_verify:
            cmd_prefix.append("--force-bad-verify")

    for pkg_item_list in pkg_params_items:
        if pkg_type == "repository":
            pkgname, version_num = pkg_item_list
            if name and pkgs is None and kwargs.get("version") and len(pkg_params) == 1:
                # Only use the 'version' param if 'name' was not specified as a
                # comma-separated list
                version_num = kwargs["version"]
        else:
            try:
                pkgname, pkgpath, version_num = pkg_item_list
            except ValueError:
                pkgname = None
                pkgpath = pkg_item_list[0]
                version_num = None

        if version_num is None:
            if pkg_type == "repository":
                if reinstall and pkgname in old:
                    to_reinstall[pkgname] = pkgname
                else:
                    targets.append(pkgname)
            else:
                targets.append(pkgpath)
        else:
            # If we are installing a package file and not one from the repo,
            # and version_num is not None, then we can assume that pkgname is
            # not None, since the only way version_num is not None is if DEB
            # metadata parsing was successful.
            if pkg_type == "repository":
                # Remove leading equals sign(s) to keep from building a pkgstr
                # with multiple equals (which would be invalid)
                version_num = version_num.lstrip("=")
                if pkgname in has_comparison:
                    candidates = _available.get(pkgname, [])
                    target = salt.utils.pkg.match_version(
                        version_num,
                        candidates,
                        cmp_func=version_cmp,
                        ignore_epoch=ignore_epoch,
                    )
                    if target is None:
                        errors.append(
                            "No version matching '{}{}' could be found "
                            "(available: {})".format(
                                pkgname,
                                version_num,
                                ", ".join(candidates) if candidates else None,
                            )
                        )
                        continue
                    else:
                        version_num = target
                pkgstr = "{}={}".format(pkgname, version_num)
            else:
                pkgstr = pkgpath

            cver = old.get(pkgname, "")
            if (
                reinstall
                and cver
                and salt.utils.versions.compare(
                    ver1=version_num, oper="==", ver2=cver, cmp_func=version_cmp
                )
            ):
                to_reinstall[pkgname] = pkgstr
            elif not cver or salt.utils.versions.compare(
                ver1=version_num, oper=">=", ver2=cver, cmp_func=version_cmp
            ):
                targets.append(pkgstr)
            else:
                downgrade.append(pkgstr)

    if fromrepo and not sources:
        log.info("Targeting repo '%s'", fromrepo)

    cmds = []
    all_pkgs = []
    if targets:
        all_pkgs.extend(targets)
        cmd = copy.deepcopy(cmd_prefix)
        cmd.extend(targets)
        cmds.append(cmd)

    if downgrade:
        cmd = copy.deepcopy(cmd_prefix)
        if pkg_type == "repository" and "--force-yes" not in cmd:
            # Downgrading requires --force-yes. Insert this before 'install'
            cmd.insert(-1, "--force-yes")
        cmd.extend(downgrade)
        cmds.append(cmd)

    if downloadonly:
        cmd.append("--download-only")

    if to_reinstall:
        all_pkgs.extend(to_reinstall)
        cmd = copy.deepcopy(cmd_prefix)
        if not sources:
            cmd.append("--reinstall")
        cmd.extend([x for x in to_reinstall.values()])
        cmds.append(cmd)

    if not cmds:
        ret = {}
    else:
        cache_valid_time = kwargs.pop("cache_valid_time", 0)
        if _refresh_db:
            refresh_db(cache_valid_time)

        env = _parse_env(kwargs.get("env"))
        env.update(DPKG_ENV_VARS.copy())

        #hold_pkgs = get_selections(state="hold").get("hold", [])
        # all_pkgs contains the argument to be passed to apt-get install, which
        # when a specific version is requested will be in the format
        # name=version.  Strip off the '=' if present so we can compare the
        # held package names against the packages we are trying to install.
        #targeted_names = [x.split("=")[0] for x in all_pkgs]
        #to_unhold = [x for x in hold_pkgs if x in targeted_names]

        #if to_unhold:
        #    unhold(pkgs=to_unhold)

        for cmd in cmds:
            out = _call_apt(cmd)
            if out["retcode"] != 0 and out["stderr"]:
                errors.append(out["stderr"])

        __context__.pop("pkg.list_pkgs", None)
        new = list_pkgs()
        ret = salt.utils.data.compare_dicts(old, new)

        for pkgname in to_reinstall:
            if pkgname not in ret or pkgname in old:
                ret.update(
                    {
                        pkgname: {
                            "old": old.get(pkgname, ""),
                            "new": new.get(pkgname, ""),
                        }
                    }
                )

        #if to_unhold:
        #    hold(pkgs=to_unhold)

    if errors:
        raise CommandExecutionError(
            "Problem encountered installing package(s)",
            info={"errors": errors, "changes": ret},
        )

    return ret

def _uninstall(action="remove", name=None, pkgs=None, **kwargs):
    """
    remove and purge do identical things but with different apt-get commands,
    this function performs the common logic.
    """
    try:
        pkg_params = __salt__["pkg_resource.parse_targets"](name, pkgs)[0]
    except MinionError as exc:
        raise CommandExecutionError(exc)

    old = list_pkgs()
    old_removed = list_pkgs(removed=True)
    targets = [x for x in pkg_params if x in old]
    if action == "purge":
        targets.extend([x for x in pkg_params if x in old_removed])
        cmd = ["apt-get", "-q", "-y", "--purge", "remove"]
    else:
        cmd = ["apt-get", "-q", "-y", action]

    if not targets:
        return {}

    cmd.extend(targets)
    env = _parse_env(kwargs.get("env"))
    env.update(DPKG_ENV_VARS.copy())
    out = _call_apt(cmd, env=env)
    if out["retcode"] != 0 and out["stderr"]:
        errors = [out["stderr"]]
    else:
        errors = []

    __context__.pop("pkg.list_pkgs", None)
    new = list_pkgs()

    changes = salt.utils.data.compare_dicts(old, new)
    ret = changes

    if errors:
        raise CommandExecutionError(
            "Problem encountered removing package(s)",
            info={"errors": errors, "changes": ret},
        )

    return ret

def autoremove(list_only=False, purge=False):
    """
    .. versionadded:: 2015.5.0

    Remove packages not required by another package using ``apt-get
    autoremove``.

    list_only : False
        Only retrieve the list of packages to be auto-removed, do not actually
        perform the auto-removal.

    purge : False
        Also remove package config data when autoremoving packages.

        .. versionadded:: 2015.8.0

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.autoremove
        salt '*' pkg.autoremove list_only=True
        salt '*' pkg.autoremove purge=True
    """
    cmd = []
    if list_only:
        ret = []
        cmd.extend(["apt-get", "--no-remove"])
        if purge:
            cmd.append("--purge")
        cmd.append("autoremove")
        out = _call_apt(cmd, ignore_retcode=True)["stdout"]
        found = False
        for line in out.splitlines():
            if found is True:
                if line.startswith(" "):
                    ret.extend(line.split())
                else:
                    found = False
            elif "The following packages will be REMOVED:" in line:
                found = True
        ret.sort()
        return ret
    else:
        old = list_pkgs()
        cmd.extend(["apt-get", "--assume-yes"])
        if purge:
            cmd.append("--purge")
        cmd.append("autoremove")
        _call_apt(cmd, ignore_retcode=True)
        __context__.pop("pkg.list_pkgs", None)
        new = list_pkgs()
        return salt.utils.data.compare_dicts(old, new)

def remove(name=None, pkgs=None, **kwargs):
    """
    .. versionchanged:: 2015.8.12,2016.3.3,2016.11.0
        On minions running systemd>=205, `systemd-run(1)`_ is now used to
        isolate commands which modify installed packages from the
        ``salt-minion`` daemon's control group. This is done to keep systemd
        from killing any apt-get/dpkg commands spawned by Salt when the
        ``salt-minion`` service is restarted. (see ``KillMode`` in the
        `systemd.kill(5)`_ manpage for more information). If desired, usage of
        `systemd-run(1)`_ can be suppressed by setting a :mod:`config option
        <salt.modules.config.get>` called ``systemd.scope``, with a value of
        ``False`` (no quotes).

    .. _`systemd-run(1)`: https://www.freedesktop.org/software/systemd/man/systemd-run.html
    .. _`systemd.kill(5)`: https://www.freedesktop.org/software/systemd/man/systemd.kill.html

    Remove packages using ``apt-get remove``.

    name
        The name of the package to be deleted.


    Multiple Package Options:

    pkgs
        A list of packages to delete. Must be passed as a python list. The
        ``name`` parameter will be ignored if this option is passed.

    .. versionadded:: 0.16.0


    Returns a dict containing the changes.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.remove <package name>
        salt '*' pkg.remove <package1>,<package2>,<package3>
        salt '*' pkg.remove pkgs='["foo", "bar"]'
    """
    return _uninstall(action="remove", name=name, pkgs=pkgs, **kwargs)

def purge(name=None, pkgs=None, **kwargs):
    """
    .. versionchanged:: 2015.8.12,2016.3.3,2016.11.0
        On minions running systemd>=205, `systemd-run(1)`_ is now used to
        isolate commands which modify installed packages from the
        ``salt-minion`` daemon's control group. This is done to keep systemd
        from killing any apt-get/dpkg commands spawned by Salt when the
        ``salt-minion`` service is restarted. (see ``KillMode`` in the
        `systemd.kill(5)`_ manpage for more information). If desired, usage of
        `systemd-run(1)`_ can be suppressed by setting a :mod:`config option
        <salt.modules.config.get>` called ``systemd.scope``, with a value of
        ``False`` (no quotes).

    .. _`systemd-run(1)`: https://www.freedesktop.org/software/systemd/man/systemd-run.html
    .. _`systemd.kill(5)`: https://www.freedesktop.org/software/systemd/man/systemd.kill.html

    Remove packages via ``apt-get -purge remove`` along with all configuration files.

    name
        The name of the package to be deleted.


    Multiple Package Options:

    pkgs
        A list of packages to delete. Must be passed as a python list. The
        ``name`` parameter will be ignored if this option is passed.

    .. versionadded:: 0.16.0


    Returns a dict containing the changes.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.purge <package name>
        salt '*' pkg.purge <package1>,<package2>,<package3>
        salt '*' pkg.purge pkgs='["foo", "bar"]'
    """
    return _uninstall(action="purge", name=name, pkgs=pkgs, **kwargs)

def upgrade(refresh=True, dist_upgrade=True, **kwargs):
    """
    .. versionchanged:: 2015.8.12,2016.3.3,2016.11.0
        On minions running systemd>=205, `systemd-run(1)`_ is now used to
        isolate commands which modify installed packages from the
        ``salt-minion`` daemon's control group. This is done to keep systemd
        from killing any apt-get/dpkg commands spawned by Salt when the
        ``salt-minion`` service is restarted. (see ``KillMode`` in the
        `systemd.kill(5)`_ manpage for more information). If desired, usage of
        `systemd-run(1)`_ can be suppressed by setting a :mod:`config option
        <salt.modules.config.get>` called ``systemd.scope``, with a value of
        ``False`` (no quotes).

    .. _`systemd-run(1)`: https://www.freedesktop.org/software/systemd/man/systemd-run.html
    .. _`systemd.kill(5)`: https://www.freedesktop.org/software/systemd/man/systemd.kill.html

    Upgrades all packages via ``apt-get upgrade`` or ``apt-get dist-upgrade``
    if  ``dist_upgrade`` is ``True``.

    Returns a dictionary containing the changes:

    .. code-block:: python

        {'<package>':  {'old': '<old-version>',
                        'new': '<new-version>'}}

    dist_upgrade
        Whether to perform the upgrade using dist-upgrade vs upgrade.  Default
        is to use dist-upgrade.

        .. versionadded:: 2014.7.0

    refresh : True
        If ``True``, the apt cache will be refreshed first. By default,
        this is ``True`` and a refresh is performed.

    cache_valid_time

        .. versionadded:: 2016.11.0

        Skip refreshing the package database if refresh has already occurred within
        <value> seconds

    download_only (or downloadonly)
        Only download the packages, don't unpack or install them. Use
        downloadonly to be in line with apt-get module.

        .. versionadded:: 2018.3.0

    force_conf_new
        Always install the new version of any configuration files.

        .. versionadded:: 2015.8.0

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.upgrade
    """
    cache_valid_time = kwargs.pop("cache_valid_time", 0)
    if salt.utils.data.is_true(refresh):
        refresh_db(cache_valid_time)

    old = list_pkgs()
    if "force_conf_new" in kwargs and kwargs["force_conf_new"]:
        dpkg_options = ["--force-confnew"]
    else:
        dpkg_options = ["--force-confold", "--force-confdef"]
    cmd = [
        "apt-get",
        "-q",
        "-y",
    ]
    for option in dpkg_options:
        cmd.append("-o")
        cmd.append("DPkg::Options::={}".format(option))

    if kwargs.get("force_yes", False):
        cmd.append("--force-yes")
    if kwargs.get("skip_verify", False):
        cmd.append("--allow-unauthenticated")
    if kwargs.get("download_only", False) or kwargs.get("downloadonly", False):
        cmd.append("--download-only")

    cmd.append("dist-upgrade" if dist_upgrade else "upgrade")
    result = _call_apt(cmd, env=DPKG_ENV_VARS.copy())
    __context__.pop("pkg.list_pkgs", None)
    new = list_pkgs()
    ret = salt.utils.data.compare_dicts(old, new)

    if result["retcode"] != 0:
        raise CommandExecutionError(
            "Problem encountered upgrading packages",
            info={"changes": ret, "result": result},
        )

    return ret

### TODO, NOT WORKS
def ___hold(name=None, pkgs=None, sources=None, **kwargs):  # pylint: disable=W0613
    # pylint: disable=W0613,E0602
    """
    .. versionadded:: 2014.7.0

    Set package in 'hold' state, meaning it will not be upgraded.

    name
        The name of the package, e.g., 'tmux'

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.hold <package name>

    pkgs
        A list of packages to hold. Must be passed as a python list.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.hold pkgs='["foo", "bar"]'
    """
    
    if not name and not pkgs and not sources:
        raise SaltInvocationError("One of name, pkgs, or sources must be specified.")
    if pkgs and sources:
        raise SaltInvocationError("Only one of pkgs or sources can be specified.")

    targets = []
    if pkgs:
        targets.extend(pkgs)
    elif sources:
        for source in sources:
            targets.append(next(iter(source)))
    else:
        targets.append(name)

    ret = {}
    for target in targets:
        if isinstance(target, dict):
            target = next(iter(target))

        ret[target] = {"name": target, "changes": {}, "result": False, "comment": ""}

        state = get_selections(pattern=target, state="hold")
        if not state:
            ret[target]["comment"] = "Package {} not currently held.".format(target)
        elif not salt.utils.data.is_true(state.get("hold", False)):
            if "test" in __opts__ and __opts__["test"]:
                ret[target].update(result=None)
                ret[target]["comment"] = "Package {} is set to be held.".format(target)
            else:
                result = set_selections(selection={"hold": [target]})
                ret[target].update(changes=result[target], result=True)
                ret[target]["comment"] = "Package {} is now being held.".format(target)
        else:
            ret[target].update(result=True)
            ret[target]["comment"] = "Package {} is already set to be held.".format(
                target
            )
    return ret

### TODO, NOT WORKS
def ___unhold(name=None, pkgs=None, sources=None, **kwargs):  
    # pylint: disable=W0613,E0602
    """
    .. versionadded:: 2014.7.0

    Set package current in 'hold' state to install state,
    meaning it will be upgraded.

    name
        The name of the package, e.g., 'tmux'

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.unhold <package name>

    pkgs
        A list of packages to unhold. Must be passed as a python list.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.unhold pkgs='["foo", "bar"]'
    """

    ### TODO

    
    if not name and not pkgs and not sources:
        raise SaltInvocationError("One of name, pkgs, or sources must be specified.")
    if pkgs and sources:
        raise SaltInvocationError("Only one of pkgs or sources can be specified.")

    targets = []
    if pkgs:
        targets.extend(pkgs)
    elif sources:
        for source in sources:
            targets.append(next(iter(source)))
    else:
        targets.append(name)

    ret = {}
    for target in targets:
        if isinstance(target, dict):
            target = next(iter(target))

        ret[target] = {"name": target, "changes": {}, "result": False, "comment": ""}

        state = get_selections(pattern=target)
        if not state:
            ret[target]["comment"] = "Package {} does not have a state.".format(target)
        elif salt.utils.data.is_true(state.get("hold", False)):
            if "test" in __opts__ and __opts__["test"]:
                ret[target].update(result=None)
                ret[target]["comment"] = "Package {} is set not to be held.".format(
                    target
                )
            else:
                result = set_selections(selection={"install": [target]})
                ret[target].update(changes=result[target], result=True)
                ret[target]["comment"] = "Package {} is no longer being held.".format(
                    target
                )
        else:
            ret[target].update(result=True)
            ret[target]["comment"] = "Package {} is already set not to be held.".format(
                target
            )
    
    return ret

def _list_pkgs_from_context(versions_as_list, contextkey, attr):
    """
    Use pkg list from __context__
    """
    return __salt__["pkg_resource.format_pkg_list"](
        __context__[contextkey], versions_as_list, attr
    )

def list_pkgs(versions_as_list=False, **kwargs):
    """
    List the packages currently installed as a dict. By default, the dict
    contains versions as a comma separated string::

        {'<package_name>': '<version>[,<version>...]'}

    versions_as_list:
        If set to true, the versions are provided as a list

        {'<package_name>': ['<version>', '<version>']}

    attr:
        If a list of package attributes is specified, returned value will
        contain them in addition to version, eg.::

        {'<package_name>': [{'version' : 'version', 'arch' : 'arch'}]}

        Valid attributes are: ``epoch``, ``version``, ``release``, ``arch``,
        ``install_date``, ``install_date_time_t``.

        If ``all`` is specified, all valid attributes will be returned.

            .. versionadded:: 2018.3.0

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.list_pkgs
        salt '*' pkg.list_pkgs attr=version,arch
        salt '*' pkg.list_pkgs attr='["version", "arch"]'
    """
    versions_as_list = salt.utils.data.is_true(versions_as_list)
    # not yet implemented or not applicable
    if any(
        [salt.utils.data.is_true(kwargs.get(x)) for x in ("removed", "purge_desired")]
    ):
        return {}

    attr = kwargs.get("attr")
    if attr is not None:
        attr = salt.utils.args.split_input(attr)

    contextkey = "pkg.list_pkgs"

    if contextkey in __context__ and kwargs.get("use_context", True):
        return _list_pkgs_from_context(versions_as_list, contextkey, attr)

    ret = {}
    cmd = [
        "rpm",
        "-qa",
        "--queryformat",
        salt.utils.pkg.rpm.QUERYFORMAT.replace("%{REPOID}", "(none)") + "\n",
    ]
    output = __salt__["cmd.run"](cmd, python_shell=False, output_loglevel="trace")
    for line in output.splitlines():
        pkginfo = salt.utils.pkg.rpm.parse_pkginfo(line, osarch=__grains__["osarch"])
        if pkginfo is not None:
            # see rpm version string rules available at https://goo.gl/UGKPNd
            pkgver = pkginfo.version
            epoch = None
            release = None
            if ":" in pkgver:
                epoch, pkgver = pkgver.split(":", 1)
            if "-" in pkgver:
                pkgver, release = pkgver.split("-", 1)
            all_attr = {
                "epoch": epoch,
                "version": pkgver,
                "release": release,
                "arch": pkginfo.arch,
                "install_date": pkginfo.install_date,
                "install_date_time_t": pkginfo.install_date_time_t,
            }
            __salt__["pkg_resource.add_pkg"](ret, pkginfo.name, all_attr)

    for pkgname in ret:
        ret[pkgname] = sorted(ret[pkgname], key=lambda d: d["version"])

    __context__[contextkey] = ret

    return __salt__["pkg_resource.format_pkg_list"](
        __context__[contextkey], versions_as_list, attr
    )

def _get_upgradable(dist_upgrade=True, **kwargs):
    """
    Utility function to get upgradable packages

    Sample return data:
    { 'pkgname': '1.2.3-45', ... }
    """

    cmd = ["apt-get", "--just-print"]
    if dist_upgrade:
        cmd.append("dist-upgrade")
    else:
        cmd.append("upgrade")
    try:
        cmd.extend(["-o", "APT::Default-Release={}".format(kwargs["fromrepo"])])
    except KeyError:
        pass

    call = _call_apt(cmd)
    if call["retcode"] != 0:
        msg = "Failed to get upgrades"
        for key in ("stderr", "stdout"):
            if call[key]:
                msg += ": " + call[key]
                break
        raise CommandExecutionError(msg)
    else:
        out = call["stdout"]

    # rexp parses lines that look like the following:
    # Conf libxfont1 (1:1.4.5-1 altlinux:testing [i386])
    rexp = re.compile("(?m)^Conf " "([^ ]+) " r"\(([^ ]+)")  # Package name  # Version
    keys = ["name", "version"]
    _get = lambda l, k: l[keys.index(k)]

    upgrades = rexp.findall(out)

    ret = {}
    for line in upgrades:
        name = _get(line, "name")
        version_num = _get(line, "version")
        ret[name] = version_num

    return ret

def list_upgrades(refresh=True, dist_upgrade=True, **kwargs):
    """
    List all available package upgrades.

    refresh
        Whether to refresh the package database before listing upgrades.
        Default: True.

    cache_valid_time

        .. versionadded:: 2016.11.0

        Skip refreshing the package database if refresh has already occurred within
        <value> seconds

    dist_upgrade
        Whether to list the upgrades using dist-upgrade vs upgrade.  Default is
        to use dist-upgrade.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.list_upgrades
    """
    cache_valid_time = kwargs.pop("cache_valid_time", 0)
    if salt.utils.data.is_true(refresh):
        refresh_db(cache_valid_time)
    return _get_upgradable(dist_upgrade, **kwargs)

def upgrade_available(name, **kwargs):
    """
    Check whether or not an upgrade is available for a given package

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.upgrade_available <package name>
    """
    return latest_version(name) != ""

def version_cmp(pkg1, pkg2, ignore_epoch=False, **kwargs):
    """
    Do a cmp-style comparison on two packages. Return -1 if pkg1 < pkg2, 0 if
    pkg1 == pkg2, and 1 if pkg1 > pkg2. Return None if there was a problem
    making the comparison.

    ignore_epoch : False
        Set to ``True`` to ignore the epoch when comparing versions

        .. versionadded:: 2015.8.10,2016.3.2

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.version_cmp '0.7.4-alt2.noarch' '0.7.5-alt1.noarch'
    """
    normalize = lambda x: str(x).split(":", 1)[0] if ignore_epoch else str(x)
    # both apt_pkg.version_compare and _cmd_quote need string arguments.
    pkg1 = str(normalize(pkg1))
    pkg2 = str(normalize(pkg2))
    try:
        if _LooseVersion(pkg1) < _LooseVersion(pkg2):
            return -1

        if _LooseVersion(pkg1) == _LooseVersion(pkg2):
            return 0

        if _LooseVersion(pkg1) > _LooseVersion(pkg2):
            return 1

    except Exception as exc:  # pylint: disable=broad-except
        log.error(exc)

    return None

def _split_repo_str(repo):
    """
    Return APT source entry as a tuple.
    """
    split = SourceEntry(repo)
    return split.type, split.architectures, split.uri, split.dist, split.comps

def list_repo_pkgs(*args, **kwargs):  # pylint: disable=unused-import
    """
    .. versionadded:: 2017.7.0

    Returns all available packages. Optionally, package names (and name globs)
    can be passed and the results will be filtered to packages matching those
    names.

    This function can be helpful in discovering the version or repo to specify
    in a :mod:`pkg.installed <salt.states.pkg.installed>` state.

    The return data will be a dictionary mapping package names to a list of
    version numbers, ordered from newest to oldest. For example:

    .. code-block:: python

        {
            'bash': ['4.3-14alt1',
                     '4.3-14alt2'],
            'nginx': ['1.10.0-0alt1',
                      '1.9.15-0alt2']
        }

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.list_repo_pkgs
        salt '*' pkg.list_repo_pkgs foo bar baz
    """
    if args:
        # Get only information about packages in args
        cmd = ["apt-cache", "show"] + [arg for arg in args]
    else:
        # Get information about all available packages
        cmd = ["apt-cache", "dump"]

    out = _call_apt(cmd, scope=False, ignore_retcode=True)

    ret = {}
    pkg_name = None

    new_pkg = re.compile("^Package: (.+)")
    for line in salt.utils.itertools.split(out["stdout"], "\n"):
        if not line.strip():
            continue
        try:
            cur_pkg = new_pkg.match(line).group(1)
        except AttributeError:
            pass
        else:
            if cur_pkg != pkg_name:
                pkg_name = cur_pkg
                continue
        comps = line.strip().split(None, 1)
        if comps[0] == "Version:":
            ret.setdefault(pkg_name, []).append(comps[1])

    return ret

def _skip_source(source):
    """
    Decide to skip source or not.

    :param source:
    :return:
    """
    if source.invalid:
        if (
            source.uri
            and source.type
            and source.type in ("deb", "deb-src", "rpm", "rpm-src")
        ):
            pieces = source.mysplit(source.line)
            if pieces[1].strip()[0] == "[":
                options = pieces.pop(1).strip("[]").split()
                if len(options) > 0:
                    log.debug(
                        "Source %s will be included although is marked invalid",
                        source.uri,
                    )
                    return False
            return True
        else:
            return True
    return False

def list_repos(**kwargs):
    """
    Lists all repos in the sources.list (and sources.lists.d) files

    CLI Example:

    .. code-block:: bash

       salt '*' pkg.list_repos
       salt '*' pkg.list_repos disabled=True
    """
    repos = {}
    sources = SourcesList()
    for source in sources.list:
        if _skip_source(source):
            continue
        repo = {}
        repo["file"] = source.file
        repo["comps"] = getattr(source, "comps", [])
        repo["disabled"] = source.disabled
        repo["dist"] = source.dist
        repo["type"] = source.type
        repo["uri"] = source.uri
        repo["line"] = source.line.strip()
        repo["architectures"] = getattr(source, "architectures", [])
        repos.setdefault(source.uri, []).append(repo)
    return repos

def get_repo(repo, **kwargs):
    """
    Display a repo from the sources.list / sources.list.d

    The repo passed in needs to be a complete repo entry.

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.get_repo "myrepo definition"
    """

    repos = list_repos()

    if repos:
        try:
            (
                repo_type,
                repo_architectures,
                repo_uri,
                repo_dist,
                repo_comps,
            ) = _split_repo_str(repo)
        except SyntaxError:
            raise CommandExecutionError(
                "Error: repo '{}' is not a well formatted definition".format(repo)
            )

        for source in repos.values():
            for sub in source:
                if (
                    sub["type"] == repo_type
                    and sub["uri"] == repo_uri
                    and sub["dist"] == repo_dist
                ):
                    if not repo_comps:
                        return sub
                    for comp in repo_comps:
                        if comp in sub.get("comps", []):
                            return sub
    return {}

def del_repo(repo, **kwargs):
    """
    Delete a repo from the sources.list / sources.list.d

    If the .list file is in the sources.list.d directory
    and the file that the repo exists in does not contain any other
    repo configuration, the file itself will be deleted.

    The repo passed in must be a fully formed repository definition
    string.

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.del_repo "myrepo definition"
    """

    call_old = _call_apt(["apt-repo"], scope=False)

    _call_apt(["apt-repo","rm",repo], scope=False)

    call_new = _call_apt(["apt-repo"], scope=False)

    if (call_old['stdout'] == call_new['stdout']):
        return "Repo '{}' doesn't exist in the sources.list(s)".format(repo)
    else:
        # explicit refresh after a repo is deleted
        refresh_db()
        return "Repo '{}' has been removed".format(repo)

### TODO add "modify" capabilities
def mod_repo(repo, **kwargs):
    """
    Adds repo, if the repo does not exist/

    refresh : True
        Enable or disable (True or False) refreshing of the apt package
        database. The previous ``refresh_db`` argument was deprecated in
        favor of ``refresh```. The ``refresh_db`` argument will still
        continue to work to ensure backwards compatibility, but please
        change to using the preferred ``refresh``.

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.mod_repo 'myrepo definition' 
        salt '*' pkg.mod_repo 'myrepo definition' 
    """
    if "refresh_db" in kwargs:
        refresh = kwargs["refresh_db"]
    else:
        refresh = kwargs.get("refresh", True)

    call_old = _call_apt(["apt-repo"], scope=False)

    _call_apt(["apt-repo","add",repo], scope=False)

    call_new = _call_apt(["apt-repo"], scope=False)


    if (call_old['stdout'] == call_new['stdout']):
        return "Repo {} already exists".format(repo)

    if refresh:
        refresh_db()

    return False

def file_list(*packages, **kwargs):
    """
    List the files that belong to a package. Not specifying any packages will
    return a list of _every_ file on the system's package database (not
    generally recommended).

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.file_list httpd
        salt '*' pkg.file_list httpd postfix
        salt '*' pkg.file_list
    """
    return __salt__["lowpkg.file_list"](*packages)

def file_dict(*packages, **kwargs):
    """
    List the files that belong to a package, grouped by package. Not
    specifying any packages will return a list of _every_ file on the system's
    package database (not generally recommended).

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.file_dict httpd
        salt '*' pkg.file_dict httpd postfix
        salt '*' pkg.file_dict
    """
    return __salt__["lowpkg.file_dict"](*packages)

def owner(*paths, **kwargs):
    """
    .. versionadded:: 2014.7.0

    Return the name of the package that owns the file. Multiple file paths can
    be passed. Like :mod:`pkg.version <salt.modules.aptpkg.version>`, if a
    single path is passed, a string will be returned, and if multiple paths are
    passed, a dictionary of file/package name pairs will be returned.

    If the file is not owned by a package, or is not present on the minion,
    then an empty string will be returned for that path.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.owner /usr/bin/apachectl
        salt '*' pkg.owner /usr/bin/apachectl /usr/bin/basename
    """
    if not paths:
        return ""
    ret = {}
    cmd_prefix = ["rpm", "-qf", "--queryformat", "%{name}"]
    for path in paths:
        ret[path] = __salt__["cmd.run_stdout"](
            cmd_prefix + [path], output_loglevel="trace", python_shell=False
        )
        if "not owned" in ret[path].lower():
            ret[path] = ""
    if len(ret) == 1:
        return next(iter(ret.values()))
    return ret

def show(*names, **kwargs):
    """
    .. versionadded:: 2019.2.0

    Runs an ``apt-cache show`` on the passed package names, and returns the
    results in a nested dictionary. The top level of the return data will be
    the package name, with each package name mapping to a dictionary of version
    numbers to any additional information returned by ``apt-cache show``.

    filter
        An optional comma-separated list (or quoted Python list) of
        case-insensitive keys on which to filter. This allows one to restrict
        the information returned for each package to a smaller selection of
        pertinent items.

    refresh : False
        If ``True``, the apt cache will be refreshed first. By default, no
        refresh is performed.

    CLI Examples:

    .. code-block:: bash

        salt myminion pkg.show gawk
        salt myminion pkg.show 'nginx-*'
        salt myminion pkg.show 'nginx-*' filter=description,provides
    """
    kwargs = salt.utils.args.clean_kwargs(**kwargs)
    refresh = kwargs.pop("refresh", False)
    filter_ = salt.utils.args.split_input(
        kwargs.pop("filter", []),
        lambda x: str(x) if not isinstance(x, str) else x.lower(),
    )
    if kwargs:
        salt.utils.args.invalid_kwargs(kwargs)

    if refresh:
        refresh_db()

    if not names:
        return {}

    result = _call_apt(["apt-cache", "show"] + list(names), scope=False)

    def _add(ret, pkginfo):
        name = pkginfo.pop("Package", None)
        version = pkginfo.pop("Version", None)
        if name is not None and version is not None:
            ret.setdefault(name, {}).setdefault(version, {}).update(pkginfo)

    def _check_filter(key):
        key = key.lower()
        return True if key in ("package", "version") or not filter_ else key in filter_

    ret = {}
    pkginfo = {}
    for line in salt.utils.itertools.split(result["stdout"], "\n"):
        line = line.strip()
        if line:
            try:
                key, val = [x.strip() for x in line.split(":", 1)]
            except ValueError:
                pass
            else:
                if _check_filter(key):
                    pkginfo[key] = val
        else:
            # We've reached a blank line, which separates packages
            _add(ret, pkginfo)
            # Clear out the pkginfo dict for the next package
            pkginfo = {}
            continue

    # Make sure to add whatever was in the pkginfo dict when we reached the end
    # of the output.
    _add(ret, pkginfo)

    return ret

def info_installed(*names, **kwargs):
    """
    Return the information of the named package(s) installed on the system.

    .. versionadded:: 2015.8.1

    names
        The names of the packages for which to return information.

    failhard
        Whether to throw an exception if none of the packages are installed.
        Defaults to True.

        .. versionadded:: 2016.11.3

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.info_installed <package1>
        salt '*' pkg.info_installed <package1> <package2> <package3> ...
        salt '*' pkg.info_installed <package1> failhard=false
    """
    kwargs = salt.utils.args.clean_kwargs(**kwargs)
    failhard = kwargs.pop("failhard", True)
    if kwargs:
        salt.utils.args.invalid_kwargs(kwargs)

    ret = dict()
    for pkg_name, pkg_nfo in __salt__["lowpkg.info"](*names, failhard=failhard).items():
        t_nfo = dict()
        if pkg_nfo.get("status", "ii")[1] != "i":
            continue  # return only packages that are really installed
        # Translate dpkg-specific keys to a common structure
        for key, value in pkg_nfo.items():
            if key == "package":
                t_nfo["name"] = value
            elif key == "origin":
                t_nfo["vendor"] = value
            elif key == "section":
                t_nfo["group"] = value
            elif key == "maintainer":
                t_nfo["packager"] = value
            elif key == "homepage":
                t_nfo["url"] = value
            elif key == "status":
                continue  # only installed pkgs are returned, no need for status
            else:
                t_nfo[key] = value

        ret[pkg_name] = t_nfo

    return ret

def list_downloaded(root=None, **kwargs):
    """
    .. versionadded:: 3000?

    List prefetched packages downloaded by apt in the local disk.

    root
        operate on a different root directory.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.list_downloaded
    """
    CACHE_DIR = "/var/cache/apt"
    if root:
        CACHE_DIR = os.path.join(root, os.path.relpath(CACHE_DIR, os.path.sep))

    ret = {}
    for root, dirnames, filenames in salt.utils.path.os_walk(CACHE_DIR):
        for filename in fnmatch.filter(filenames, "*.rpm"):
            package_path = os.path.join(root, filename)
            pkg_info = __salt__["lowpkg.bin_pkg_info"](package_path)
            pkg_timestamp = int(os.path.getctime(package_path))
            ret.setdefault(pkg_info["name"], {})[pkg_info["version"]] = {
                "path": package_path,
                "size": os.path.getsize(package_path),
                "creation_date_time_t": pkg_timestamp,
                "creation_date_time": datetime.datetime.utcfromtimestamp(
                    pkg_timestamp
                ).isoformat(),
            }
    return ret

def services_need_restart(**kwargs):
    """
    .. versionadded:: 3003

    List services that use files which have been changed by the
    package manager. It might be needed to restart them.

    Requires checkrestart from the debian-goodies package.

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.services_need_restart
    """
    if not salt.utils.path.which_bin(["checkrestart"]):
        raise CommandNotFoundError(
            "'checkrestart' is needed. It is part of the 'debian-goodies' "
            "package which can be installed from official repositories."
        )

    cmd = ["checkrestart", "--machine", "--package"]
    services = set()

    cr_output = __salt__["cmd.run_stdout"](cmd, python_shell=False)
    for line in cr_output.split("\n"):
        if not line.startswith("SERVICE:"):
            continue
        end_of_name = line.find(",")
        service = line[8:end_of_name]  # skip "SERVICE:"
        services.add(service)

    return list(services)

def kernel_update(downloadonly=False, **kwargs):
    """
    Delete a repo from the sources.list / sources.list.d

    If the .list file is in the sources.list.d directory
    and the file that the repo exists in does not contain any other
    repo configuration, the file itself will be deleted.

    The repo passed in must be a fully formed repository definition
    string.

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.del_repo "myrepo definition"
    """

    cmd = ["update-kernel"]

    if downloadonly:
        cmd.append("--dry-run")

    cmd.append("-y")

    ret = _call_apt(cmd, scope=False)

    return ret['stdout']

def kernel_list(**kwargs):
    """
        Show list of avaiable kernels to update or switch
    """
    ret = _call_apt(["update-kernel","--list"], scope=False)
    return ret['stdout']

def kernel_remove(**kwargs):
    """
    Delete a repo from the sources.list / sources.list.d

    If the .list file is in the sources.list.d directory
    and the file that the repo exists in does not contain any other
    repo configuration, the file itself will be deleted.

    The repo passed in must be a fully formed repository definition
    string.

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.del_repo "myrepo definition"
    """

    cmd = ["remove-old-kernels"]
    cmd.append("-y")

    ret = _call_apt(cmd, scope=False)

    return ret['stdout']

### USED EXTERNAL
def normalize_name(name):
    """
    Strips the architecture from the specified package name, if necessary.
    Circumstances where this would be done include:

    * If the arch is 32 bit and the package name ends in a 32-bit arch.
    * If the arch matches the OS arch, or is ``noarch``.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.normalize_name zsh.x86_64
    """
    try:
        arch = name.rsplit(PKG_ARCH_SEPARATOR, 1)[-1]
        if arch not in salt.utils.pkg.rpm.ARCHES + ("noarch",):
            return name
    except ValueError:
        return name
    if arch in (__grains__["osarch"], "noarch") or salt.utils.pkg.rpm.check_32(
        arch, osarch=__grains__["osarch"]
    ):
        return name[: -(len(arch) + 1)]
    return name