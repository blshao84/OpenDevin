"""Test the EventStreamRuntime, which connects to the RuntimeClient running in the sandbox."""

import asyncio
import os
import time
from unittest.mock import patch

import pytest
from pytest import TempPathFactory

from opendevin.core.config import AppConfig, SandboxConfig, load_from_env
from opendevin.core.logger import opendevin_logger as logger
from opendevin.events import EventStream
from opendevin.events.action import (
    BrowseURLAction,
    CmdRunAction,
    FileReadAction,
    FileWriteAction,
    IPythonRunCellAction,
)
from opendevin.events.observation import (
    BrowserOutputObservation,
    CmdOutputObservation,
    ErrorObservation,
    FileReadObservation,
    FileWriteObservation,
    IPythonRunCellObservation,
)
from opendevin.runtime.client.runtime import EventStreamRuntime
from opendevin.runtime.plugins import AgentSkillsRequirement, JupyterRequirement
from opendevin.runtime.server.runtime import ServerRuntime
from opendevin.storage import get_file_store


@pytest.fixture(autouse=True)
def print_method_name(request):
    print('\n########################################################################')
    print(f'Running test: {request.node.name}')
    print('########################################################################')


@pytest.fixture
def temp_dir(tmp_path_factory: TempPathFactory) -> str:
    return str(tmp_path_factory.mktemp('test_runtime'))


TEST_RUNTIME = os.getenv('TEST_RUNTIME', 'both')
PY3_FOR_TESTING = '/opendevin/miniforge3/bin/mamba run -n base python3'


# This assures that all tests run together for each runtime, not alternating between them,
# which caused them to fail previously.
@pytest.fixture(scope='module')
def box_class(request):
    time.sleep(1)
    runtime = TEST_RUNTIME
    if runtime.lower() == 'eventstream':
        return EventStreamRuntime
    elif runtime.lower() == 'server':
        return ServerRuntime
    else:
        return pytest.param([EventStreamRuntime, ServerRuntime])


# TODO: We will change this to `run_as_user` when `ServerRuntime` is deprecated.
# since `EventStreamRuntime` supports running as an arbitrary user.
@pytest.fixture(scope='module', params=[True, False])
def run_as_devin(request):
    time.sleep(1)
    return request.param


@pytest.fixture(scope='module', params=[True, False])
def enable_auto_lint(request):
    time.sleep(1)
    return request.param


@pytest.fixture(scope='module', params=['ubuntu:22.04', 'debian:11'])
def container_image(request):
    time.sleep(1)
    return request.param


async def _load_runtime(
    temp_dir,
    box_class,
    run_as_devin: bool = True,
    enable_auto_lint: bool = False,
    container_image: str | None = None,
):
    sid = 'test'
    cli_session = 'main_test'
    # AgentSkills need to be initialized **before** Jupyter
    # otherwise Jupyter will not access the proper dependencies installed by AgentSkills
    plugins = [AgentSkillsRequirement(), JupyterRequirement()]
    config = AppConfig(
        workspace_base=temp_dir,
        workspace_mount_path=temp_dir,
        sandbox=SandboxConfig(use_host_network=True),
    )
    load_from_env(config, os.environ)
    config.run_as_devin = run_as_devin
    config.sandbox.enable_auto_lint = enable_auto_lint

    file_store = get_file_store(config.file_store, config.file_store_path)
    event_stream = EventStream(cli_session, file_store)

    if container_image is not None:
        config.sandbox.container_image = container_image

    if box_class == EventStreamRuntime:
        # NOTE: we will use the default container image specified in the config.sandbox
        # if it is an official od_runtime image.
        cur_container_image = config.sandbox.container_image
        if 'od_runtime' not in cur_container_image:
            cur_container_image = 'ubuntu:22.04'
            logger.warning(
                f'`{config.sandbox.container_image}` is not an od_runtime image. Will use `{cur_container_image}` as the container image for testing.'
            )

        runtime = EventStreamRuntime(
            config=config,
            event_stream=event_stream,
            sid=sid,
            plugins=plugins,
            # NOTE: we probably don't have a default container image `/sandbox` for the event stream runtime
            # Instead, we will pre-build a suite of container images with OD-runtime-cli installed.
            container_image=cur_container_image,
        )
        await runtime.ainit()
    elif box_class == ServerRuntime:
        runtime = ServerRuntime(
            config=config, event_stream=event_stream, sid=sid, plugins=plugins
        )
        await runtime.ainit()
        from opendevin.runtime.tools import (
            RuntimeTool,  # deprecate this after ServerRuntime is deprecated
        )

        runtime.init_runtime_tools(
            [RuntimeTool.BROWSER],
            is_async=False,
            runtime_tools_config={},
        )
    else:
        raise ValueError(f'Invalid box class: {box_class}')
    await asyncio.sleep(1)
    return runtime


@pytest.mark.asyncio
async def test_env_vars_os_environ(temp_dir, box_class, run_as_devin):
    with patch.dict(os.environ, {'SANDBOX_ENV_FOOBAR': 'BAZ'}):
        runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

        obs: CmdOutputObservation = await runtime.run_action(
            CmdRunAction(command='env')
        )
        print(obs)

        obs: CmdOutputObservation = await runtime.run_action(
            CmdRunAction(command='echo $FOOBAR')
        )
        print(obs)
        assert obs.exit_code == 0, 'The exit code should be 0.'
        assert (
            obs.content.strip().split('\n\r')[0].strip() == 'BAZ'
        ), f'Output: [{obs.content}] for {box_class}'

        await runtime.close()
        await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_env_vars_runtime_add_env_vars(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)
    await runtime.add_env_vars({'QUUX': 'abc"def'})

    obs: CmdOutputObservation = await runtime.run_action(
        CmdRunAction(command='echo $QUUX')
    )
    print(obs)
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert (
        obs.content.strip().split('\r\n')[0].strip() == 'abc"def'
    ), f'Output: [{obs.content}] for {box_class}'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_env_vars_runtime_add_empty_dict(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    prev_obs = await runtime.run_action(CmdRunAction(command='env'))
    assert prev_obs.exit_code == 0, 'The exit code should be 0.'
    print(prev_obs)

    await runtime.add_env_vars({})

    obs = await runtime.run_action(CmdRunAction(command='env'))
    assert obs.exit_code == 0, 'The exit code should be 0.'
    print(obs)
    assert (
        obs.content == prev_obs.content
    ), 'The env var content should be the same after adding an empty dict.'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_env_vars_runtime_add_multiple_env_vars(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)
    await runtime.add_env_vars({'QUUX': 'abc"def', 'FOOBAR': 'xyz'})

    obs: CmdOutputObservation = await runtime.run_action(
        CmdRunAction(command='echo $QUUX $FOOBAR')
    )
    print(obs)
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert (
        obs.content.strip().split('\r\n')[0].strip() == 'abc"def xyz'
    ), f'Output: [{obs.content}] for {box_class}'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_env_vars_runtime_add_env_vars_overwrite(temp_dir, box_class):
    with patch.dict(os.environ, {'SANDBOX_ENV_FOOBAR': 'BAZ'}):
        runtime = await _load_runtime(temp_dir, box_class)
        await runtime.add_env_vars({'FOOBAR': 'xyz'})

        obs: CmdOutputObservation = await runtime.run_action(
            CmdRunAction(command='echo $FOOBAR')
        )
        print(obs)
        assert obs.exit_code == 0, 'The exit code should be 0.'
        assert (
            obs.content.strip().split('\r\n')[0].strip() == 'xyz'
        ), f'Output: [{obs.content}] for {box_class}'

        await runtime.close()
        await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_bash_command_pexcept(temp_dir, box_class, run_as_devin):
    runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

    # We set env var PS1="\u@\h:\w $"
    # and construct the PEXCEPT prompt base on it.
    # When run `env`, bad implementation of CmdRunAction will be pexcepted by this
    # and failed to pexcept the right content, causing it fail to get error code.
    obs = await runtime.run_action(CmdRunAction(command='env'))

    # For example:
    # 02:16:13 - opendevin:DEBUG: client.py:78 - Executing command: env
    # 02:16:13 - opendevin:DEBUG: client.py:82 - Command output: PYTHONUNBUFFERED=1
    # CONDA_EXE=/opendevin/miniforge3/bin/conda
    # [...]
    # LC_CTYPE=C.UTF-8
    # PS1=\u@\h:\w $
    # 02:16:13 - opendevin:DEBUG: client.py:89 - Executing command for exit code: env
    # 02:16:13 - opendevin:DEBUG: client.py:92 - Exit code Output:
    # CONDA_DEFAULT_ENV=base

    # As long as the exit code is 0, the test will pass.
    assert isinstance(
        obs, CmdOutputObservation
    ), 'The observation should be a CmdOutputObservation.'
    assert obs.exit_code == 0, 'The exit code should be 0.'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_simple_cmd_ipython_and_fileop(temp_dir, box_class, run_as_devin):
    runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

    # Test run command
    action_cmd = CmdRunAction(command='ls -l')
    logger.info(action_cmd, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_cmd)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    assert 'total 0' in obs.content

    # Test run ipython
    test_code = "print('Hello, `World`!\\n')"
    action_ipython = IPythonRunCellAction(code=test_code)
    logger.info(action_ipython, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_ipython)
    assert isinstance(obs, IPythonRunCellObservation)

    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.content.strip() == 'Hello, `World`!'

    # Test read file (file should not exist)
    action_read = FileReadAction(path='hello.sh')
    logger.info(action_read, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_read)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, ErrorObservation)
    assert 'File not found' in obs.content

    # Test write file
    action_write = FileWriteAction(content='echo "Hello, World!"', path='hello.sh')
    logger.info(action_write, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_write)
    assert isinstance(obs, FileWriteObservation)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert obs.content == ''
    if box_class == ServerRuntime:
        assert obs.path == 'hello.sh'
    else:
        # event stream runtime will always use absolute path
        assert obs.path == '/workspace/hello.sh'

    # Test read file (file should exist)
    action_read = FileReadAction(path='hello.sh')
    logger.info(action_read, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_read)
    assert isinstance(
        obs, FileReadObservation
    ), 'The observation should be a FileReadObservation.'
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert obs.content == 'echo "Hello, World!"\n'
    if box_class == ServerRuntime:
        assert obs.path == 'hello.sh'
    else:
        assert obs.path == '/workspace/hello.sh'

    # clean up
    action = CmdRunAction(command='rm -rf hello.sh')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_simple_browse(temp_dir, box_class, run_as_devin):
    runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

    # Test browse
    action_cmd = CmdRunAction(
        command=f'{PY3_FOR_TESTING} -m http.server 8000 > server.log 2>&1 &'
    )
    logger.info(action_cmd, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_cmd)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    assert '[1]' in obs.content

    action_cmd = CmdRunAction(command='sleep 5 && cat server.log')
    logger.info(action_cmd, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_cmd)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0

    action_browse = BrowseURLAction(url='http://localhost:8000')
    logger.info(action_browse, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_browse)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, BrowserOutputObservation)
    assert 'http://localhost:8000' in obs.url
    assert obs.status_code == 200
    assert not obs.error
    assert obs.open_pages_urls == ['http://localhost:8000/']
    assert obs.active_page_index == 0
    assert obs.last_browser_action == 'goto("http://localhost:8000")'
    assert obs.last_browser_action_error == ''
    assert 'Directory listing for /' in obs.content
    assert 'server.log' in obs.content

    # clean up
    action = CmdRunAction(command='rm -rf server.log')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_single_multiline_command(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    action = CmdRunAction(command='echo \\\n -e "foo"')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert 'foo' in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_multiline_echo(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    action = CmdRunAction(command='echo -e "hello\nworld"')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert 'hello\r\nworld' in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_runtime_whitespace(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    action = CmdRunAction(command='echo -e "\\n\\n\\n"')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert '\r\n\r\n\r\n' in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_multiple_multiline_commands(temp_dir, box_class, run_as_devin):
    cmds = [
        'ls -l',
        'echo -e "hello\nworld"',
        """
echo -e "hello it\\'s me"
""".strip(),
        """
echo \\
    -e 'hello' \\
    -v
""".strip(),
        """
echo -e 'hello\\nworld\\nare\\nyou\\nthere?'
""".strip(),
        """
echo -e 'hello
world
are
you\\n
there?'
""".strip(),
        """
echo -e 'hello
world "
'
""".strip(),
    ]
    joined_cmds = '\n'.join(cmds)

    runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

    action = CmdRunAction(command=joined_cmds)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0, 'The exit code should be 0.'

    assert 'total 0' in obs.content
    assert 'hello\r\nworld' in obs.content
    assert "hello it\\'s me" in obs.content
    assert 'hello -v' in obs.content
    assert 'hello\r\nworld\r\nare\r\nyou\r\nthere?' in obs.content
    assert 'hello\r\nworld\r\nare\r\nyou\r\n\r\nthere?' in obs.content
    assert 'hello\r\nworld "\r\n' in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_no_ps2_in_output(temp_dir, box_class, run_as_devin):
    """Test that the PS2 sign is not added to the output of a multiline command."""
    runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

    action = CmdRunAction(command='echo -e "hello\nworld"')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    if box_class == ServerRuntime:
        # the extra PS2 '>' is NOT handled by the ServerRuntime
        assert 'hello\r\nworld' in obs.content
        assert '>' in obs.content
        assert obs.content.count('>') == 1
    else:
        assert 'hello\r\nworld' in obs.content
        assert '>' not in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_multiline_command_loop(temp_dir, box_class):
    # https://github.com/OpenDevin/OpenDevin/issues/3143

    runtime = await _load_runtime(temp_dir, box_class)

    init_cmd = """
mkdir -p _modules && \
for month in {01..04}; do
    for day in {01..05}; do
        touch "_modules/2024-${month}-${day}-sample.md"
    done
done
echo "created files"
"""
    action = CmdRunAction(command=init_cmd)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert 'created files' in obs.content

    follow_up_cmd = """
for file in _modules/*.md; do
    new_date=$(echo $file | sed -E 's/2024-(01|02|03|04)-/2024-/;s/2024-01/2024-08/;s/2024-02/2024-09/;s/2024-03/2024-10/;s/2024-04/2024-11/')
    mv "$file" "$new_date"
done
echo "success"
"""
    action = CmdRunAction(command=follow_up_cmd)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert 'success' in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_cmd_run(temp_dir, box_class, run_as_devin):
    runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

    action = CmdRunAction(command='ls -l')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    assert 'total 0' in obs.content

    action = CmdRunAction(command='mkdir test')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0

    action = CmdRunAction(command='ls -l')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    if run_as_devin:
        assert 'opendevin' in obs.content
    else:
        assert 'root' in obs.content
    assert 'test' in obs.content

    action = CmdRunAction(command='touch test/foo.txt')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0

    action = CmdRunAction(command='ls -l test')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    assert 'foo.txt' in obs.content

    # clean up: this is needed, since CI will not be
    # run as root, and this test may leave a file
    # owned by root
    action = CmdRunAction(command='rm -rf test')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_run_as_user_correct_home_dir(temp_dir, box_class, run_as_devin):
    runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

    action = CmdRunAction(command='cd ~ && pwd')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    if run_as_devin:
        assert '/home/opendevin' in obs.content
    else:
        assert '/root' in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_multi_cmd_run_in_single_line(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    action = CmdRunAction(command='pwd && ls -l')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    assert '/workspace' in obs.content
    assert 'total 0' in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_stateful_cmd(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    action = CmdRunAction(command='mkdir test')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0, 'The exit code should be 0.'

    action = CmdRunAction(command='cd test')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0, 'The exit code should be 0.'

    action = CmdRunAction(command='pwd')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert '/workspace/test' in obs.content

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_failed_cmd(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    action = CmdRunAction(command='non_existing_command')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code != 0, 'The exit code should not be 0 for a failed command.'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_ipython_multi_user(temp_dir, box_class, run_as_devin):
    runtime = await _load_runtime(temp_dir, box_class, run_as_devin)

    # Test run ipython
    # get username
    test_code = "import os; print(os.environ['USER'])"
    action_ipython = IPythonRunCellAction(code=test_code)
    logger.info(action_ipython, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_ipython)
    assert isinstance(obs, IPythonRunCellObservation)

    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    if run_as_devin:
        assert 'opendevin' in obs.content
    else:
        assert 'root' in obs.content

    # print pwd
    test_code = 'import os; print(os.getcwd())'
    action_ipython = IPythonRunCellAction(code=test_code)
    logger.info(action_ipython, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_ipython)
    assert isinstance(obs, IPythonRunCellObservation)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.content.strip() == '/workspace'

    # write a file
    test_code = "with open('test.txt', 'w') as f: f.write('Hello, world!')"
    action_ipython = IPythonRunCellAction(code=test_code)
    logger.info(action_ipython, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_ipython)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, IPythonRunCellObservation)
    assert obs.content.strip() == '[Code executed successfully with no output]'

    # check file owner via bash
    action = CmdRunAction(command='ls -alh test.txt')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0
    if run_as_devin:
        # -rw-r--r-- 1 opendevin root 13 Jul 28 03:53 test.txt
        assert 'opendevin' in obs.content.split('\r\n')[0]
        assert 'root' in obs.content.split('\r\n')[0]
    else:
        # -rw-r--r-- 1 root root 13 Jul 28 03:53 test.txt
        assert 'root' in obs.content.split('\r\n')[0]

    # clean up
    action = CmdRunAction(command='rm -rf test')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_ipython_simple(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    # Test run ipython
    # get username
    test_code = 'print(1)'
    action_ipython = IPythonRunCellAction(code=test_code)
    logger.info(action_ipython, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_ipython)
    assert isinstance(obs, IPythonRunCellObservation)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.content.strip() == '1'


async def _test_ipython_agentskills_fileop_pwd_impl(
    runtime: ServerRuntime | EventStreamRuntime, enable_auto_lint: bool
):
    # remove everything in /workspace
    action = CmdRunAction(command='rm -rf /workspace/*')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0

    action = CmdRunAction(command='mkdir test')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0

    action = IPythonRunCellAction(code="create_file('hello.py')")
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, IPythonRunCellObservation)
    assert obs.content.replace('\r\n', '\n').strip().split('\n') == (
        '[File: /workspace/hello.py (1 lines total)]\n'
        '(this is the beginning of the file)\n'
        '1|\n'
        '(this is the end of the file)\n'
        '[File hello.py created.]\n'
    ).strip().split('\n')

    action = CmdRunAction(command='cd test')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0

    # This should create a file in the current working directory
    # i.e., /workspace/test/hello.py instead of /workspace/hello.py
    action = IPythonRunCellAction(code="create_file('hello.py')")
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, IPythonRunCellObservation)
    assert obs.content.replace('\r\n', '\n').strip().split('\n') == (
        '[File: /workspace/test/hello.py (1 lines total)]\n'
        '(this is the beginning of the file)\n'
        '1|\n'
        '(this is the end of the file)\n'
        '[File hello.py created.]\n'
    ).strip().split('\n')

    if enable_auto_lint:
        # edit file, but make a mistake in indentation
        action = IPythonRunCellAction(
            code="insert_content_at_line('hello.py', 1, '  print(\"hello world\")')"
        )
        logger.info(action, extra={'msg_type': 'ACTION'})
        obs = await runtime.run_action(action)
        logger.info(obs, extra={'msg_type': 'OBSERVATION'})
        assert isinstance(obs, IPythonRunCellObservation)
        assert obs.content.replace('\r\n', '\n').strip().split('\n') == (
            """
[Your proposed edit has introduced new syntax error(s). Please understand the errors and retry your edit command.]
ERRORS:
/workspace/test/hello.py:1:3: E999 IndentationError: unexpected indent
[This is how your edit would have looked if applied]
-------------------------------------------------
(this is the beginning of the file)
1|  print("hello world")
(this is the end of the file)
-------------------------------------------------

[This is the original code before your edit]
-------------------------------------------------
(this is the beginning of the file)
1|
(this is the end of the file)
-------------------------------------------------
Your changes have NOT been applied. Please fix your edit command and try again.
You either need to 1) Specify the correct start/end line arguments or 2) Correct your edit code.
DO NOT re-run the same failed edit command. Running it again will lead to the same error.
"""
        ).strip().split('\n')

    # edit file with correct indentation
    action = IPythonRunCellAction(
        code="insert_content_at_line('hello.py', 1, 'print(\"hello world\")')"
    )
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, IPythonRunCellObservation)
    assert obs.content.replace('\r\n', '\n').strip().split('\n') == (
        """
[File: /workspace/test/hello.py (1 lines total after edit)]
(this is the beginning of the file)
1|print("hello world")
(this is the end of the file)
[File updated (edited at line 1). Please review the changes and make sure they are correct (correct indentation, no duplicate lines, etc). Edit the file again if necessary.]
"""
    ).strip().split('\n')

    action = CmdRunAction(command='rm -rf /workspace/*')
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0


@pytest.mark.asyncio
async def test_ipython_agentskills_fileop_pwd(temp_dir, box_class, enable_auto_lint):
    """Make sure that cd in bash also update the current working directory in ipython."""

    runtime = await _load_runtime(
        temp_dir, box_class, enable_auto_lint=enable_auto_lint
    )
    await _test_ipython_agentskills_fileop_pwd_impl(runtime, enable_auto_lint)
    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.skipif(
    TEST_RUNTIME.lower() == 'eventstream',
    reason='Skip this if we want to test EventStreamRuntime',
)
@pytest.mark.skipif(
    os.environ.get('TEST_IN_CI', 'false').lower() == 'true',
    # FIXME: There's some weird issue with the CI environment.
    reason='Skip this if in CI.',
)
@pytest.mark.asyncio
async def test_ipython_agentskills_fileop_pwd_agnostic_sandbox(
    temp_dir, enable_auto_lint, container_image
):
    """Make sure that cd in bash also update the current working directory in ipython."""

    runtime = await _load_runtime(
        temp_dir,
        # NOTE: we only test for ServerRuntime, since EventStreamRuntime is image agnostic by design.
        ServerRuntime,
        enable_auto_lint=enable_auto_lint,
        container_image=container_image,
    )
    await _test_ipython_agentskills_fileop_pwd_impl(runtime, enable_auto_lint)
    await runtime.close()
    await asyncio.sleep(1)
