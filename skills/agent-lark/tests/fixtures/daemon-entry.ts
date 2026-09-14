// A daemon process that never talks to Feishu or herdr: the CLI tests spawn
// this instead of the real thing. State lives wherever AGENT_LARK_HOME says.
//
// AGENT_LARK_FAKE_CONNECT shapes the fake handshake for the paths that wait
// on `connected`: `fail` keeps every attempt failing, `fail:<n>` only the
// first n (the retry interval is 50 ms). Unset, the first attempt succeeds.
import { runDaemon } from '../../src/daemon.js';
import { createFakeChannel, type FakeChannelOptions } from './fake-channel.js';
import { createFakeHerdr } from './fake-herdr.js';

process.env.AGENT_LARK_APP_ID ??= 'cli_fake';
process.env.AGENT_LARK_APP_SECRET ??= 'fake-secret';

const channelOpts: FakeChannelOptions = {};
const shape = process.env.AGENT_LARK_FAKE_CONNECT;
if (shape) {
  const failures = shape === 'fail' ? Infinity : Number(shape.replace(/^fail:/, ''));
  let attempts = 0;
  channelOpts.connect = async () => {
    attempts += 1;
    if (attempts <= failures) throw new Error(`fake handshake ${attempts} refused`);
  };
}

const daemon = await runDaemon({
  createChannel: () => createFakeChannel(channelOpts).channel,
  herdr: createFakeHerdr().deps,
  connectRetryMs: 50,
});
await daemon.done;
