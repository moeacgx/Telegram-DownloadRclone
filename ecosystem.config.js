const path = require('path');

const projectRoot = __dirname;
const venvPath = path.join(projectRoot, 'venv');
const pythonBinary =
  process.platform === 'win32'
    ? path.join(venvPath, 'Scripts', 'python.exe')
    : path.join(venvPath, 'bin', 'python');

module.exports = {
  apps: [
    {
      name: 'tg-channel-bot',
      cwd: projectRoot,
      script: path.join(projectRoot, 'bot.py'),
      interpreter: pythonBinary,
      interpreter_args: '-u',
      env: {
        VIRTUAL_ENV: venvPath,
        PATH:
          process.platform === 'win32'
            ? `${path.join(venvPath, 'Scripts')};${process.env.PATH}`
            : `${path.join(venvPath, 'bin')}:${process.env.PATH}`,
        PYTHONUNBUFFERED: '1'
      },
      watch: false,
      max_memory_restart: '3000M',
      autorestart: true
    }
  ]
};
