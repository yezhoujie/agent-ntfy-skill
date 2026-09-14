// herdr as the daemon sees it, without spawning anything.
import type { HerdrDeps } from '../../src/daemon.js';
import { findPaneForProject, type AgentInfo, type PromptOutcome } from '../../src/herdr.js';

export interface FakeHerdr {
  deps: HerdrDeps;
  agents: AgentInfo[];
  prompts: Array<{ paneId: string; text: string }>;
  /** What promptPane answers; defaults to accepted. */
  outcome: PromptOutcome;
}

export function createFakeHerdr(opts: { agents?: AgentInfo[]; outcome?: PromptOutcome } = {}): FakeHerdr {
  const fake: FakeHerdr = {
    agents: opts.agents ?? [],
    prompts: [],
    outcome: opts.outcome ?? { ok: true },
    deps: {
      agentList: async () => fake.agents,
      promptPane: async (paneId, text) => {
        fake.prompts.push({ paneId, text });
        return fake.outcome;
      },
      findPaneForProject,
    },
  };
  return fake;
}
