// A daemon process that never talks to Feishu or herdr: the CLI tests spawn
// this instead of the real thing. State lives wherever AGENT_LARK_HOME says.
import { runDaemon } from '../../src/daemon.js';
import { createFakeChannel } from './fake-channel.js';
import { createFakeHerdr } from './fake-herdr.js';

process.env.AGENT_LARK_APP_ID ??= 'cli_fake';
process.env.AGENT_LARK_APP_SECRET ??= 'fake-secret';

const daemon = await runDaemon({
  createChannel: () => createFakeChannel().channel,
  herdr: createFakeHerdr().deps,
  connectRetryMs: 50,
});
await daemon.done;
