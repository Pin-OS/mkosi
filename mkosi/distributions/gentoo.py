# SPDX-License-Identifier: LGPL-2.1+

import contextlib
import fcntl
import os
import re
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path
from textwrap import dedent
from typing import Dict, Generator, List, Sequence

from mkosi.backend import (
    ARG_DEBUG,
    MkosiConfig,
    MkosiException,
    MkosiPrinter,
    MkosiState,
    complete_step,
    die,
    run_workspace_command,
    safe_tar_extract,
)
from mkosi.distributions import DistributionInstaller
from mkosi.install import copy_path, open_close
from mkosi.remove import unlink_try_hard

ARCHITECTURES = {
    "x86_64": ("amd64", "arch/x86/boot/bzImage"),
    # TODO:
    "aarch64": ("arm64", "arch/arm64/boot/Image.gz"),
    # TODO:
    "armv7l": ("arm", "arch/arm/boot/zImage"),
}


@contextlib.contextmanager
def flock_path(path: Path) -> Generator[int, None, None]:
    with open_close(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd


class Gentoo:
    arch_profile: Path
    baselayout_use: Path
    DEFAULT_NSPAWN_PARAMS: List[str]
    emerge_default_opts: List[str]
    arch: str
    emerge_vars: Dict[str, str]
    # sys-boot and sys-kernel mainly for boot
    pkgs_boot: List[str]
    # @system set (https://wiki.gentoo.org/wiki/System_set_(Portage))
    pkgs_sys: List[str]
    # filesystem packages (dosfstools, btrfs, squashfs, etc)
    pkgs_fs: List[str]
    UNINSTALL_IGNORE: List[str]
    root: Path
    portage_cfg_dir: Path
    profile_path: Path
    custom_profile_path: Path
    ebuild_sh_env_dir: Path
    dracut_atom = "sys-kernel/dracut"

    EMERGE_UPDATE_OPTS = [
        "--update",
        "--tree",
        "--changed-use",
        "--newuse",
        "--deep",
        "--with-bdeps=y",
        "--complete-graph-if-new-use=y",
    ]

    UNINSTALL_IGNORE = ["/bin", "/sbin", "/lib", "/lib64"]

    portage_use_flags = [
        "systemd",  # 'systemd' is a dependancy
        "initramfs",
        "git",  # 'git' for sync-type=git
        "symlink",  # 'symlink' for kernel
        "sdl",
        "-filecaps",
        "-savedconfig",
        "-split-bin",
        "-split-sbin",
        "-split-usr",
    ]

    # TODO: portage_features.add("ccache"), this shall expedite the builds
    portage_features = [
        # -user* are required for access to USER_CONFIG_PATH
        "-userfetch",
        "-userpriv",
        "-usersync",
        "-usersandbox",
        "-sandbox",
        "-pid-sandbox",  # -pid-sandbox is required for cross-compile scenarios
        "-network-sandbox",
        "parallel-install",
        "buildpkg",
        "binpkg-multi-instance",
        "-binpkg-docompress",
        "getbinpkg",
        "-candy",
    ]

    @staticmethod
    def try_import_portage() -> Dict[str, str]:
        NEED_PORTAGE_MSG = "You need portage(5) for Gentoo"
        PORTAGE_INSTALL_INSTRUCTIONS = """\
        # Following is known to work on most systemd-based systems:
        sudo tee /usr/lib/sysusers.d/acct-user-portage.conf > /dev/null <<- EOF
        # /usr/lib/sysusers.d/portage.conf
        u portage - "Portage system user" /var/lib/portage/home -
        EOF

        sudo systemd-sysusers --no-pager

        sudo install --owner=portage --group=portage --mode=0755 --directory /var/db/repos
        sudo install --owner=portage --group=portage --mode=0755 --directory /etc/portage/repos.conf
        sudo install --owner=portage --group=portage --mode=0755 --directory /var/cache/binpkgs

        sudo tee /etc/portage/repos.conf/eselect-repo.conf > /dev/null <<- EOF
        [gentoo]
        location = /var/db/repos/gentoo
        sync-type = git
        sync-uri = https://anongit.gentoo.org/git/repo/gentoo.git
        EOF

        git clone https://anongit.gentoo.org/git/proj/portage.git --depth=1
        cd portage
        tee setup.cfg > /dev/null <<- EOF
        [build_ext]
        portage-ext-modules=true
        EOF

        python setup.py build_ext --inplace --portage-ext-modules

        sudo python setup.py install

        sudo ln -s --relative /var/db/repos/gentoo/profiles/default/linux/amd64/17.1/no-multilib/systemd /etc/portage/make.profile
        """
        try:
            from portage.const import (  # type: ignore
                CUSTOM_PROFILE_PATH,
                EBUILD_SH_ENV_DIR,
                PROFILE_PATH,
                USER_CONFIG_PATH,
            )
        except ImportError as e:
            MkosiPrinter.warn(NEED_PORTAGE_MSG)
            MkosiPrinter.info(PORTAGE_INSTALL_INSTRUCTIONS)
            raise MkosiException(e)

        return dict(profile_path=PROFILE_PATH,
                    custom_profile_path=CUSTOM_PROFILE_PATH,
                    ebuild_sh_env_dir=EBUILD_SH_ENV_DIR,
                    portage_cfg_dir=USER_CONFIG_PATH)

    def __init__(
        self,
        state: MkosiState,
    ) -> None:

        ret = self.try_import_portage()

        from portage.package.ebuild.config import config as portage_cfg  # type: ignore

        self.portage_cfg = portage_cfg(config_root=str(state.root), target_root=str(state.root),
                                  sysroot=str(state.root), eprefix=None)

        PORTAGE_MISCONFIGURED_MSG = "You have portage(5) installed but it's probably missing defaults, bailing out"
        # we check for PORTDIR, but we could check for any other one
        if self.portage_cfg['PORTDIR'] is None:
            die(PORTAGE_MISCONFIGURED_MSG)

        self.profile_path = state.root / ret["profile_path"]
        self.custom_profile_path = state.root / ret["custom_profile_path"]
        self.ebuild_sh_env_dir = state.root / ret["ebuild_sh_env_dir"]
        self.portage_cfg_dir = state.root / ret["portage_cfg_dir"]

        self.portage_cfg_dir.mkdir(parents=True, exist_ok=True)

        self.DEFAULT_NSPAWN_PARAMS = [
            "--capability=CAP_SYS_ADMIN,CAP_MKNOD",
            f"--bind={self.portage_cfg['PORTDIR']}",
            f"--bind={self.portage_cfg['DISTDIR']}",
            f"--bind={self.portage_cfg['PKGDIR']}",
        ]

        jobs = os.cpu_count() or 1
        self.emerge_default_opts = [
            "--buildpkg=y",
            "--usepkg=y",
            "--keep-going=y",
            f"--jobs={jobs}",
            f"--load-average={jobs-1}",
            "--nospinner",
        ]
        if "build-script" in ARG_DEBUG:
            self.emerge_default_opts += ["--verbose",
                                         "--quiet=n",
                                         "--quiet-fail=n"]
        else:
            self.emerge_default_opts += ["--quiet-build", "--quiet"]

        self.arch, _ = ARCHITECTURES[state.config.architecture or "x86_64"]

        #######################################################################
        # GENTOO_UPSTREAM : we only support systemd profiles! and only the
        # no-multilib flavour of those, for now;
        # GENTOO_UPSTREAM : wait for fix upstream:
        # https://bugs.gentoo.org/792081
        #######################################################################
        # GENTOO_DONTMOVE : could be done inside set_profile, however
        # stage3_fetch() will be needing this if we want to allow users to pick
        # profile
        #######################################################################
        self.arch_profile = Path(f"profiles/default/linux/{self.arch}/{state.config.release}/systemd")

        self.pkgs_sys = ["@world"]

        self.pkgs_fs = ["sys-fs/dosfstools"]

        if not state.do_run_build_script and state.config.bootable:
            self.pkgs_boot += ["sys-kernel/installkernel-systemd-boot",
                               "sys-kernel/gentoo-kernel-bin",
                               "sys-firmware/edk2-ovmf"]

        self.emerge_vars = {
            "BOOTSTRAP_USE": " ".join(self.portage_use_flags),
            "FEATURES": " ".join(self.portage_features),
            "UNINSTALL_IGNORE": " ".join(self.UNINSTALL_IGNORE),
            "USE": " ".join(self.portage_use_flags),
        }

        self.sync_portage_tree(state)
        self.set_profile(state.config)
        self.set_default_repo()
        self.unmask_arch()
        self.provide_patches()
        self.set_useflags()
        self.mkosi_conf()
        self.baselayout(state)
        self.fetch_fix_stage3(state, state.root)
        self.update_stage3(state)
        self.depclean(state)

    def sync_portage_tree(self, state: MkosiState) -> None:
        self.invoke_emerge(state, inside_stage3=False, actions=["--sync"])

    def fetch_fix_stage3(self, state: MkosiState, root: Path) -> None:
        """usrmerge tracker bug: https://bugs.gentoo.org/690294"""

        # e.g.:
        # http://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3.txt

        stage3tsf_path_url = urllib.parse.urljoin(
            self.portage_cfg["GENTOO_MIRRORS"].partition(" ")[0],
            f"releases/{self.arch}/autobuilds/latest-stage3.txt",
        )

        ###########################################################
        # GENTOO_UPSTREAM: wait for fix upstream:
        # https://bugs.gentoo.org/690294
        # and more... so we can gladly escape all this hideousness!
        ###########################################################
        with urllib.request.urlopen(stage3tsf_path_url) as r:
            args_profile = "nomultilib"
            # 20210711T170538Z/stage3-amd64-nomultilib-systemd-20210711T170538Z.tar.xz 214470580
            regexp = f"^[0-9TZ]+/stage3-{self.arch}-{args_profile}-systemd-[0-9TZ]+[.]tar[.]xz"
            all_lines = r.readlines()
            for line in all_lines:
                m = re.match(regexp, line.decode("utf-8"))
                if m:
                    stage3_tar = Path(m.group(0))
                    break
            else:
                die("profile names changed upstream?")

        stage3_url_path = urllib.parse.urljoin(
            self.portage_cfg["GENTOO_MIRRORS"],
            f"releases/{self.arch}/autobuilds/{stage3_tar}",
        )
        stage3_tar_path = self.portage_cfg["DISTDIR"] / stage3_tar
        stage3_tmp_extract = stage3_tar_path.with_name(
                                            stage3_tar.name + ".tmp")
        if not stage3_tar_path.is_file():
            MkosiPrinter.print_step(f"Fetching {stage3_url_path}")
            stage3_tar_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(stage3_url_path, stage3_tar_path)

        stage3_tmp_extract.mkdir(parents=True, exist_ok=True)

        with flock_path(stage3_tmp_extract):
            if not stage3_tmp_extract.joinpath(".cache_isclean").exists():
                with tarfile.open(stage3_tar_path) as tfd:
                    MkosiPrinter.print_step(f"Extracting {stage3_tar.name} to "
                                            f"{stage3_tmp_extract}")
                    safe_tar_extract(tfd, stage3_tmp_extract, numeric_owner=True)

                # REMOVEME : pathetic attempt have this merged :)
                # remove once upstream ships the current *baselayout-999*
                # version alternative would be to mount /sys as tmpfs when
                # invoking emerge inside stage3; we don't want that.
                self.invoke_emerge(state, inside_stage3=True,
                        opts=["--unmerge"], pkgs=["sys-apps/baselayout"])

                unlink_try_hard(stage3_tmp_extract.joinpath("dev"))
                unlink_try_hard(stage3_tmp_extract.joinpath("proc"))
                unlink_try_hard(stage3_tmp_extract.joinpath("sys"))

                stage3_tmp_extract.joinpath("bin/awk").unlink()
                root.joinpath("usr/bin/awk").symlink_to("gawk")

                stage3_tmp_extract.joinpath(".cache_isclean").touch()

        MkosiPrinter.print_step(f"Copying {stage3_tmp_extract} to {root}")
        copy_path(stage3_tmp_extract.joinpath("usr"),
                  root.joinpath("usr"))
        dirs = ["bin", "lib", "lib64"]
        for d in dirs:
            copy_path(stage3_tmp_extract.joinpath(d),
                      root.joinpath(f"usr/{d}"))
        dirs = ["etc", "var/db", "var/lib", "var/cache"]
        for d in dirs:
            copy_path(stage3_tmp_extract.joinpath(d), root.joinpath(d))

        copy_path(stage3_tmp_extract.joinpath("sbin"),
                  root.joinpath("usr/bin"))

    def set_profile(self, config: MkosiConfig) -> None:
        if not self.profile_path.is_symlink():
            MkosiPrinter.print_step(f"{config.distribution} setting Profile")
            self.profile_path.symlink_to(
                self.portage_cfg["PORTDIR"] / self.arch_profile)

    def set_default_repo(self) -> None:
        eselect_repo_conf = self.portage_cfg_dir / "repos.conf"
        eselect_repo_conf.mkdir(exist_ok=True)
        eselect_repo_conf.joinpath("eselect-repo.conf").write_text(
            dedent(
                f"""\
                [gentoo]
                location = {self.portage_cfg["PORTDIR"]}
                sync-uri = https://anongit.gentoo.org/git/repo/gentoo.git
                sync-type = git
                sync-dept = 1
                """
            )
        )

    def unmask_arch(self) -> None:
        package_accept_keywords = self.portage_cfg_dir / "package.accept_keywords"
        package_accept_keywords.mkdir(exist_ok=True)

        package_accept_keywords.joinpath("mkosi").write_text(
            dedent(
                # USE=homed is still in ~ARCH,
                # ~ARCH (for a given ARCH) is the unstable version of the
                # package, `Beta` if you like. more here:
                # https://wiki.gentoo.org/wiki//etc/portage/package.accept_keywords
                f"""\
                sys-auth/pambase ~{self.arch}
                # sys-kernel/gentoo-kernel-bin ~{self.arch}
                # virtual/dist-kernel ~{self.arch}
                """
            )
        )
        # -999 means install from git
        package_accept_keywords.joinpath("baselayout").write_text(
            dedent("""
                # REMOVE: once upstream has moved this to stable
                # releases of baselayout
                # https://gitweb.gentoo.org/proj/baselayout.git/commit/?id=57c250e24c70f8f9581860654cdec0d049345292
                =sys-apps/baselayout-9999 **
            """)
        )

        package_accept_keywords.joinpath("bug765208").write_text(
                                    f"<{self.dracut_atom}-56 ~{self.arch}\n")

    def provide_patches(self) -> None:
        patches_dir = self.portage_cfg_dir / "patches"
        patches_dir.mkdir(exist_ok=True)

    def set_useflags(self) -> None:
        self.custom_profile_path.mkdir(exist_ok=True)
        self.custom_profile_path.joinpath("use.force").write_text(
            dedent(
                """\
               -split-bin
               -split-sbin
               -split-usr
               """)
        )

        package_use = self.portage_cfg_dir / "package.use"
        package_use.mkdir(exist_ok=True)

        self.baselayout_use = package_use.joinpath("baselayout")
        self.baselayout_use.write_text("sys-apps/baselayout build\n")
        package_use.joinpath("systemd").write_text(
            # repart for usronly
            dedent(
                """\
                # sys-apps/systemd http
                # sys-apps/systemd cgroup-hybrid

                # MKOSI: Failed to open "/usr/lib/systemd/boot/efi": No such file or directory
                sys-apps/systemd gnuefi

                # sys-apps/systemd -pkcs11
                # sys-apps/systemd importd lzma

                sys-apps/systemd homed cryptsetup -pkcs11
                # See: https://bugs.gentoo.org/832167
                # sys-apps/systemd[homed] should depend on sys-auth/pambase[homed]
                sys-auth/pambase homed

                # MKOSI: usronly
                sys-apps/systemd repart
                # sys-apps/systemd -cgroup-hybrid
                # sys-apps/systemd vanilla
                # sys-apps/systemd policykit
                # MKOSI: make sure we're init (no openrc)
                sys-apps/systemd sysv-utils
                """
            )
        )

    def mkosi_conf(self) -> None:
        package_env = self.portage_cfg_dir / "package.env"
        package_env.mkdir(exist_ok=True)
        self.ebuild_sh_env_dir.mkdir(exist_ok=True)

        # apply whatever we put in mkosi_conf to runs invokation of emerge
        package_env.joinpath("mkosi.conf").write_text("*/*    mkosi.conf\n")

        # we use this so we don't need to touch upstream files.
        # we also use this for documenting build environment.
        emerge_vars_str = ""
        emerge_vars_str += "\n".join(f'{k}="${{{k}}} {v}"' for k, v in self.emerge_vars.items())

        self.ebuild_sh_env_dir.joinpath("mkosi.conf").write_text(
            dedent(
                f"""\
                # MKOSI: these were used during image creation...
                # and some more! see under package.*/
                #
                # usrmerge (see all under profile/)
                {emerge_vars_str}
                """
            )
        )

    def invoke_emerge(
        self,
        state: MkosiState,
        inside_stage3: bool = True,
        pkgs: Sequence[str] = (),
        actions: Sequence[str] = (),
        opts: Sequence[str] = (),
    ) -> None:
        if not inside_stage3:
            from _emerge.main import emerge_main  # type: ignore

            PREFIX_OPTS: List[str] = []
            if "--sync" not in actions:
                PREFIX_OPTS = [
                    f"--config-root={state.root.resolve()}",
                    f"--root={state.root.resolve()}",
                    f"--sysroot={state.root.resolve()}",
                ]

            MkosiPrinter.print_step(f"Invoking emerge(1) pkgs={pkgs} "
                                    f"actions={actions} outside stage3")
            emerge_main([*pkgs, *opts, *actions] + PREFIX_OPTS + self.emerge_default_opts)
        else:
            cmd = ["/usr/bin/emerge", *pkgs, *self.emerge_default_opts, *opts, *actions]

            MkosiPrinter.print_step("Invoking emerge(1) inside stage3")
            run_workspace_command(
                state,
                cmd,
                network=True,
                env=self.emerge_vars,
                nspawn_params=self.DEFAULT_NSPAWN_PARAMS,
            )

    def baselayout(self, state: MkosiState) -> None:
        # TOTHINK: sticky bizness when when image profile != host profile
        # REMOVE: once upstream has moved this to stable releases of baselaouy
        # https://gitweb.gentoo.org/proj/baselayout.git/commit/?id=57c250e24c70f8f9581860654cdec0d049345292
        self.invoke_emerge(state, inside_stage3=False,
                           opts=["--nodeps"],
                           pkgs=["=sys-apps/baselayout-9999"])

    def update_stage3(self, state: MkosiState) -> None:
        # exclude baselayout, it expects /sys/.keep but nspawn mounts host's
        # /sys for us without the .keep file.
        opts = self.EMERGE_UPDATE_OPTS + ["--exclude",
                                          "sys-apps/baselayout"]
        self.invoke_emerge(state, pkgs=self.pkgs_sys, opts=opts)

        # FIXME?: without this we get the following
        # Synchronizing state of sshd.service with SysV service script with /lib/systemd/systemd-sysv-install.
        # Executing: /lib/systemd/systemd-sysv-install --root=/var/tmp/mkosi-2b6snh_u/root enable sshd
        # chroot: failed to run command ‘/usr/sbin/update-rc.d’: No such file or directory
        state.root.joinpath("etc/init.d/sshd").unlink()

        # "build" USE flag can go now, next time users do an update they will
        # safely merge baselayout without that flag and it should be fine at
        # that point.
        self.baselayout_use.unlink()

    def depclean(self, state: MkosiState) -> None:
        self.invoke_emerge(state, actions=["--depclean"])

    def _dbg(self, state: MkosiState) -> None:
        """this is for dropping into shell to see what's wrong"""

        cmdline = ["/bin/sh"]
        run_workspace_command(
            state,
            cmdline,
            network=True,
            nspawn_params=self.DEFAULT_NSPAWN_PARAMS,
        )


class GentooInstaller(DistributionInstaller):
    @classmethod
    def cache_path(cls) -> List[str]:
        return ["var/cache/binpkgs"]

    @staticmethod
    def kernel_image(name: str, architecture: str) -> Path:
        _, kimg_path = ARCHITECTURES[architecture]
        return Path(f"usr/src/linux-{name}") / kimg_path

    @classmethod
    @complete_step("Installing Gentoo…")
    def install(cls, state: "MkosiState") -> None:
        # this will fetch/fix stage3 tree and portage confgired for mkosi
        gentoo = Gentoo(state)

        if gentoo.pkgs_fs:
            gentoo.invoke_emerge(state, pkgs=gentoo.pkgs_fs)

        if not state.do_run_build_script and state.config.bootable:
            # The gentoo stage3 tarball includes packages that may block chosen
            # pkgs_boot. Using Gentoo.EMERGE_UPDATE_OPTS for opts allows the
            # package manager to uninstall blockers.
            gentoo.invoke_emerge(state, pkgs=gentoo.pkgs_boot, opts=Gentoo.EMERGE_UPDATE_OPTS)

        if state.config.packages:
            gentoo.invoke_emerge(state, pkgs=state.config.packages)

        if state.do_run_build_script:
            gentoo.invoke_emerge(state, pkgs=state.config.build_packages)
