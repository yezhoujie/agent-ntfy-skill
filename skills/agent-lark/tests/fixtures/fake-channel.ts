// An in-memory stand-in for the Feishu channel: records what the daemon sends,
// exposes the event handlers it registered so a test can play the phone.
import type { CardActionEvent, CardActionResponse, EventMap, NormalizedMessage } from '@larksuite/channel';
import type { ChannelLike } from '../../src/daemon.js';

export type SentItem = { chatId: string; input: unknown } | { update: string; card: object };

export interface FakeChannelOptions {
  /** Replaces connect(); lets a test make the first attempts fail. */
  connect?: () => Promise<void>;
  listChats?: ChannelLike['listChats'];
  getChatInfo?: ChannelLike['getChatInfo'];
  createChat?: ChannelLike['createChat'];
  addReaction?: ChannelLike['addReaction'];
  rawClient?: unknown;
}

export interface FakeChannel {
  channel: ChannelLike;
  sent: SentItem[];
  handlers: Partial<EventMap>;
  connectCalls: number;
  disconnectCalls: number;
  policy: unknown[];
  /** Deliver a message as if the human typed it in the group. */
  message(partial: Partial<NormalizedMessage> & { chatId: string; content: string }): Promise<void>;
  /** Tap a button on a card. */
  cardAction(evt: CardActionEvent): Promise<void | CardActionResponse>;
}

const notImplemented = (name: string) => async (): Promise<never> => {
  throw new Error(`fake channel: ${name} not implemented`);
};

export function createFakeChannel(opts: FakeChannelOptions = {}): FakeChannel {
  const fake: FakeChannel = {
    sent: [],
    handlers: {},
    connectCalls: 0,
    disconnectCalls: 0,
    policy: [],
    channel: undefined as unknown as ChannelLike,
    async message(partial) {
      const evt: NormalizedMessage = {
        messageId: `om_${fake.sent.length}_${Date.now()}`,
        chatType: 'group',
        senderId: 'ou_human',
        senderIsBot: false,
        rawContentType: 'text',
        resources: [],
        mentions: [],
        mentionAll: false,
        mentionedBot: false,
        createTime: Date.now(),
        ...partial,
      };
      await fake.handlers.message?.(evt);
    },
    async cardAction(evt) {
      return fake.handlers.cardAction?.(evt);
    },
  };
  let connected = false;
  const rawClient =
    opts.rawClient ??
    new Proxy(
      {},
      {
        get(_target, prop) {
          throw new Error(`fake channel: rawClient.${String(prop)} not implemented`);
        },
      },
    );
  const channel = {
    on(nameOrMap: unknown, handler?: unknown): () => void {
      if (typeof nameOrMap === 'string') {
        (fake.handlers as Record<string, unknown>)[nameOrMap] = handler;
      } else {
        Object.assign(fake.handlers, nameOrMap as Partial<EventMap>);
      }
      return () => {};
    },
    async connect(): Promise<void> {
      fake.connectCalls += 1;
      if (opts.connect) await opts.connect();
      connected = true;
    },
    async disconnect(): Promise<void> {
      fake.disconnectCalls += 1;
      connected = false;
    },
    updatePolicy(partial: unknown): void {
      fake.policy.push(partial);
    },
    getConnectionStatus() {
      return connected ? { state: 'connected' as const, reconnectAttempts: 0 } : undefined;
    },
    async send(chatId: string, input: unknown) {
      fake.sent.push({ chatId, input });
      return { messageId: `om_${fake.sent.length}` };
    },
    async updateCard(messageId: string, card: object): Promise<void> {
      fake.sent.push({ update: messageId, card });
    },
    listChats: opts.listChats ?? notImplemented('listChats'),
    getChatInfo: opts.getChatInfo ?? notImplemented('getChatInfo'),
    createChat: opts.createChat ?? notImplemented('createChat'),
    addReaction: opts.addReaction ?? notImplemented('addReaction'),
    downloadResourceToFile: notImplemented('downloadResourceToFile'),
    rawClient,
  };
  fake.channel = channel as unknown as ChannelLike;
  return fake;
}
