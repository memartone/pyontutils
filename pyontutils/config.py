import os
import stat
import yaml
from pathlib import Path
from tempfile import gettempdir
from functools import wraps
import appdirs
from pyontutils.utils import TermColors as tc, makeSimpleLogger
from pyontutils.utils import get_working_dir

checkout_ok = 'NIFSTD_CHECKOUT_OK' in os.environ
pyontutils_config_path = Path(appdirs.user_config_dir('pyontutils'))
default_config = pyontutils_config_path / 'devconfig.yaml'
working_dir = get_working_dir(__file__)
if working_dir is None:
    # we are not in git, we are probably testing or installed by a user
    default_curies = pyontutils_config_path / 'curie_map.yaml'
    # hardcoding the default api here to avoid importing the scigraph client
    default_scigraph_api = 'https://scicrunch.org/api/1/scigraph'
else:
    default_curies = working_dir / 'nifstd' / 'scigraph' / 'curie_map.yaml'
    default_scigraph_api = 'http://localhost:9000/scigraph'

# needed to override for local testing
PYONTUTILS_DEVCONFIG = Path(os.environ.get('PYONTUTILS_DEVCONFIG', default_config))

log = makeSimpleLogger('config')


def get_api_key():
    try:
        return os.environ['SCICRUNCH_API_KEY']
    except KeyError:
        if 'https' in devconfig.scigraph_api and 'scicrunch.org' in devconfig.scigraph_api:
            maybe_key = devconfig.secrets('scicrunch', 'api', devconfig.scigraph_api_user)
            if maybe_key:
                return maybe_key


class dproperty(property):
    default = None


class dstr(str):
    default = None

    @property
    def isDefault(self):
        return self == self.default


def default(value):
    def decorator(function, default_value=value):
        dv = dstr(default_value)
        dv.default = default_value

        @wraps(function)
        def inner(*args, **kwargs):
            try:
                raw_out = function(*args, **kwargs)
                if raw_out is None:
                    return

                out = dstr(raw_out)
                out.default = default_value
                return out
            except (TypeError, KeyError, FileNotFoundError) as e:
                if default_value is None:
                    return
                else:
                    return dv

        pinner = dproperty(inner)
        pinner.default = default_value
        return pinner

    return decorator


tempdir = gettempdir()


class Secrets:
    def __init__(self, devconfig):
        self.devconfig = devconfig
        if self.exists:
            fstat = os.stat(self.filename)
            mode = oct(stat.S_IMODE(fstat.st_mode))
            if mode != '0o600' and mode != '0o700':
                raise FileNotFoundError(f'Your secrets file {self.filename} '
                                        f'can be read by the whole world! {mode}')

    @property
    def filename(self):
        return self.devconfig.secrets_file

    @property
    def exists(self):
        e = Path(self.filename).exists()
        if not e:
            log.warning(f'secrets file {self.filename} does not exist. '
                        'You can set an alternate path under the secrets_file: '
                        f'variable in {self.devconfig.config_file}')

        return e

    @property
    def name_id_map(self):
        # sometimes the easiest solution is just to read from disk every single time
        if self.exists:
            with open(self.filename, 'rt') as f:
                return yaml.safe_load(f)

    def __call__(self, *names):
        if self.exists:
            nidm = self.name_id_map
            # NOTE under these circumstances this pattern is ok because anyone
            # or anything who can call this function can access the secrets file.
            # Normally this would be an EXTREMELY DANGEROUS PATTERN. Because short
            # secrets could be exposted by brute force, but in thise case it is ok
            # because it is more important to alert the user that they have just
            # tried to use a secret as a name and that it might be in their code.
            def all_values(d):
                for v in d.values():
                    if isinstance(v, dict):
                        yield from all_values(v)
                    else:
                        yield v

            av = set(all_values(nidm))
            current = nidm
            for name in names:
                if name in av:
                    ANGRY = '*' * len(name)
                    raise ValueError(f'WHY ARE YOU TRYING TO USE A SECRET {ANGRY} AS A NAME!?')
                else:
                    current = current[name]

            if isinstance(current, dict):
                raise ValueError(f'Your secret path is incomplete. Keys are {sorted(current.keys())}')

            return current

class DevConfig:
    skip = 'config', 'write', 'ontology_remote_repo', 'v', 'secrets'
    secrets = None  # prevent AttributeError during bootstrap

    class MissingRepoError(Exception):
        """ Use this if a repo at a path is missing in a script """

    class NoResourcesError(Exception):
        """ if devconfig.resources does not exist
            raise this when exiting a script """

    def __init__(self, config_file=PYONTUTILS_DEVCONFIG):
        self._override = {}
        if not isinstance(config_file, Path):
            config_file = Path(config_file).expanduser().resolve()

        self.config_file = config_file
        olrd = lambda: Path(self.git_local_base, self.ontology_repo).as_posix()
        self.__class__.ontology_local_repo.default = olrd

    @property
    def secrets(self):
        if not hasattr(self, '__secrets'):
            self.__secrets = Secrets(self)
        return self.__secrets

    @property
    def config(self):
        """ Allows changing the config on the fly """
        # TODO more efficient to read once and put watch on the file
        config = {}
        if self.config_file.exists():
            with open(self.config_file.as_posix(), 'rt') as f:  # 3.5/pypy3 can't open Path directly
                config = {k:self._override[k] if
                        k in self._override else
                        v for k, v in yaml.safe_load(f).items()}

        return config

    @property
    def _config(self):
        # FIXME make it clear that this is read only...
        out = {}  # do it this way to read first
        for name in dir(self):
            if not name.startswith('_') and name not in self.skip:
                thing = getattr(self.__class__, name, None)
                if isinstance(thing, property):
                    if name in self._override:
                        out[name] = self._override[name]
                    else:
                        out[name] = getattr(self, name)

        return out

    def write(self, file=None):
        if file is None:
            file = (PYONTUTILS_DEVCONFIG).as_posix()

        config = self.config
        new_config = {k:str(v) for k, v in self._config.items()}
        config.update(new_config)  # roundtrip keys that we don't manage in this class

        if config:
            with open(file, 'wt') as f:
                yaml.dump(config, f, default_flow_style=False)
            if self._override:
                self._override = {}
        else:
            raise ValueError('devconfig is empty?!')

        return file

    def _colluser(self, path):
        path = Path(path)
        prefix = path.home()
        return '~' + path.as_posix().strip(prefix.as_posix())

    @default(Path('~/pyontutils-secrets.yaml').expanduser().as_posix())
    def secrets_file(self):
        return self.config['secrets_file']

    @secrets_file.setter
    def secrets_file(self, value):
        if isinstance(value, Path):
            value = value.as_posix()
        self._override['secrets_file'] = value
        self.write(self.config_file.as_posix())

    @default(default_curies.as_posix())
    def curies(self):
        return self.config['curies']

    @default((working_dir / 'nifstd' / 'patches' / 'patches.yaml').as_posix() if working_dir else None)
    def patch_config(self):
        return self.config['patch_config']

    @default('https://github.com')
    def git_remote_base(self):
        return self.config['git_remote_base']

    @default(tempdir)
    def git_local_base(self):
        return os.path.expanduser(self.config['git_local_base'])

    @git_local_base.setter
    def git_local_base(self, value):
        if isinstance(value, Path):
            value = value.as_posix()
        self._override['git_local_base'] = value
        self.write(self.config_file.as_posix())

    @default('uri.interlex.org')
    def ilx_host(self):
        return self.config['ilx_host']

    @default('')
    def ilx_port(self):
        return self.config['ilx_port']

    @default('neurons')
    def neurons_branch(self):
        return self.config['neurons_branch']

    @default('SciCrunch')
    def ontology_org(self):
        return self.config['ontology_org']

    @default('NIF-Ontology')
    def ontology_repo(self):
        return self.config['ontology_repo']

    @property
    def ontology_remote_repo(self):
        return os.path.join(self.git_remote_base, self.ontology_org, self.ontology_repo)

    @dproperty
    def ontology_local_repo(self):
        def add_default(default=self.__class__.ontology_local_repo.default()):
            out = dstr(default)
            out.default = default
            return out

        try:
            olr = Path(self.config['ontology_local_repo']).resolve()
            if olr:
                if olr == self._ontology_local_repo:
                    return add_default(olr)
                else:
                    return add_default(self._ontology_local_repo.as_posix())
            else:
                raise ValueError('config entry for ontology_local_repo is empty')
        except (KeyError, ValueError, TypeError, FileNotFoundError) as e:
            # key for line missing from config
            # value from where we raise above if olr is the empty string ''
            # type for key present but value is None
            # file not found for path does not exist
            return add_default(self._ontology_local_repo) if self._ontology_local_repo else add_default()

    @property
    def _maybe_repo(self):
        return Path(self.git_local_base, self.ontology_repo).absolute()

    @property
    def _ontology_local_repo(self):
        try:
            stated_repo = Path(self.config['ontology_local_repo'])
        except (KeyError, TypeError, FileNotFoundError) as e:
            stated_repo = Path('/dev/null/does-not-exist')

        maybe_repo = self._maybe_repo
        if stated_repo.exists():
            return stated_repo
        elif maybe_repo.exists():
            return maybe_repo
        else:
            maybe_start = Path(__file__).parent.parent.parent.absolute()
            maybe_base = maybe_start
            fsroot = Path('/')
            while maybe_base != fsroot:
                maybe_repo = maybe_base / self.ontology_repo
                if maybe_repo.exists():
                    log.info(tc.blue('INFO:') + f'Ontology repository found at {maybe_repo}')
                    return maybe_repo
                else:
                    maybe_base = maybe_base.parent
            else:
                log.warning(tc.red('WARNING:') +
                            f'No repository found in any parent directory of {maybe_start}')

        return Path('/dev/null/does-not-exist')  # seems reaonsable ...

    @default((working_dir / 'nifstd' /'resources').as_posix() if working_dir else None)
    def resources(self):
        return self.config['resources']

    @default(default_scigraph_api)
    def scigraph_api(self):
        return self.config['scigraph_api']

    @scigraph_api.setter
    def scigraph_api(self, value):
        self._override['scigraph_api'] = value
        self.write(self.config_file.as_posix())

    @default(None)
    def scigraph_api_user(self):
        return self.config['scigraph_api_user']

    @default((working_dir / 'nifstd' / 'scigraph' / 'graphload.yaml').as_posix()
             if working_dir else None)
    def scigraph_graphload(self):
        return self.config['scigraph_graphload']

    @default((working_dir / 'nifstd' / 'scigraph' / 'services.yaml').as_posix()
             if working_dir else None)
    def scigraph_services(self):
        return self.config['scigraph_services']

    @default((working_dir / 'nifstd' / 'scigraph' / 'start.sh').as_posix()
             if working_dir else None)
    def scigraph_start(self):
        return self.config['scigraph_start']

    @default((working_dir / 'nifstd' / 'scigraph' / 'stop.sh').as_posix()
             if working_dir else None)
    def scigraph_stop(self):
        return self.config['scigraph_stop']

    @default((working_dir / 'nifstd' / 'scigraph' / 'scigraph-services.service').as_posix()
             if working_dir else None)
    def scigraph_systemd(self):
        return self.config['scigraph_systemd']

    @default((working_dir / 'nifstd' / 'scigraph' / 'scigraph-services.conf').as_posix()
             if working_dir else None)
    def scigraph_java(self):
        return self.config['scigraph_java']

    @default('/tmp')
    def zip_location(self):
        return self.config['zip_location']

    def _check_resources(self):
        if self.resources is None:
            raise self.NoResourcesError('devconfig.resources is not set, cannot continue')

    def _check_ontology_local_repo(self):
        path = Path(self.ontology_local_repo)
        if not path.exists():
            raise self.MissingRepoError(f'repo for {path} does not exist')


    def __repr__(self):
        return (f'DevConfig {self.config_file}\n' +
                '\n'.join(f'{k:<20} {v}'
                          for k, v in {k:getattr(self, k)
                                       for k in dir(self) if
                                       not k.startswith('_') and
                                       k not in ('config', 'write', 'config_file', 'secrets') and
                                       isinstance(getattr(self.__class__, k), property)}.items()))


devconfig = DevConfig()


def bootstrap_config():
    if not devconfig.config_file.exists():
        # scigraph api
        maybe_key = get_api_key()
        if maybe_key:
            from pyontutils.scigraph_client import BASEPATH
            devconfig.scigraph_api = BASEPATH
        else:
            devconfig.scigraph_api = devconfig.scigraph_api.default

        # ontology repo
        p1 = Path(__file__).resolve().absolute().parent.parent.parent
        p2 = Path(devconfig.git_local_base).resolve().absolute()
        print(p1, p2)
        if (p1 / devconfig.ontology_repo).exists():
            if p1 != p2:
                devconfig.git_local_base = p1
    else:
        log.info(f'config already exists at {devconfig.config_file}')


def main():
    from IPython import embed
    print(repr(devconfig))

if __name__ == '__main__':
    main()
