// An in-memory stand-in for the Feishu channel: records what the daemon sends,
// exposes the event handlers it registered so a test can play the phone.
import type { CardActionEvent, CardActionResponse, EventMap, NormalizedMessage } from '@larksuite/channel';
import type { ChannelLike } from '../../src/daemon.js';

export type SentItem = { chatId: string; input: unknown } | { update: string; card: object };

export interface FakeChannelOptions {
  /** Replaces connect(); lets a test make the first attempts fail. */
  connect?: () => Promise<void>;
  /** Replaces the REST card update (the record in `sent` is still kept); lets a test make it hang. */
  updateCard?: (messageId: string, card: object) => Promise<void>;
  getChatInfo?: ChannelLike['getChatInfo'];
  createChat?: ChannelLike['createChat'];
  addReaction?: ChannelLike['addReaction'];
  /** Replaces the attachment download; a test can make it succeed without a file or fail. */
  downloadResourceToFile?: ChannelLike['downloadResourceToFile'];
  /** Answers `rawClient.im.v1.chat.update`; every call is also recorded in `renames`. */
  chatUpdate?: (req: ChatUpdateRequest) => Promise<ChatUpdateResult>;
  /** Answers `rawClient.im.v1.chat.delete`; every call is also recorded in `deletes`. */
  chatDelete?: (req: ChatDeleteRequest) => Promise<ChatDeleteResult>;
  /** Answers `rawClient.im.v1.chat.list`, one page per call; `pageOf()` builds the usual answers. */
  chatList?: (req: ChatListRequest) => Promise<ChatListResult>;
  /** Answers `rawClient.im.v1.message.urgentApp`; every call is also recorded in `urgents`. */
  urgentApp?: (req: UrgentAppRequest) => Promise<UrgentAppResult>;
  rawClient?: unknown;
}

export interface UrgentAppRequest {
  path: { message_id: string };
  params: { user_id_type: string };
  data: { user_id_list: string[] };
}
export type UrgentAppResult = { code?: number; msg?: string; data?: { invalid_user_id_list: string[] } };

export interface ChatUpdateRequest {
  path: { chat_id: string };
  data: { name?: string; description?: string };
}
export type ChatUpdateResult = { code?: number; msg?: string };
export interface ChatDeleteRequest {
  path: { chat_id: string };
}
export type ChatDeleteResult = { code?: number; msg?: string };
export interface ChatListRequest {
  params: { page_size?: number; page_token?: string };
}
export type ChatListResult = {
  code?: number;
  msg?: string;
  data?: { items?: Array<{ chat_id?: string; name?: string }>; has_more?: boolean; page_token?: string };
};
/** A `chat.list` answer listing `ids`, `pageSize` per page, following `page_token` the way Feishu does. */
export function pageOf(ids: string[], pageSize = 100): (req: ChatListRequest) => Promise<ChatListResult> {
  return async ({ params }) => {
    const from = Number(params.page_token ?? 0);
    const items = ids.slice(from, from + pageSize).map((chat_id) => ({ chat_id, name: chat_id }));
    const hasMore = from + pageSize < ids.length;
    return { code: 0, data: { items, has_more: hasMore, ...(hasMore ? { page_token: String(from + pageSize) } : {}) } };
  };
}

export interface FakeChannel {
  channel: ChannelLike;
  sent: SentItem[];
  handlers: Partial<EventMap>;
  connectCalls: number;
  disconnectCalls: number;
  policy: unknown[];
  /** Group updates (name / description) issued through rawClient, oldest first. */
  renames: Array<{ chatId: string; name: string | undefined; description: string | undefined }>;
  /** Groups dissolved through rawClient, oldest first. */
  deletes: string[];
  /** `chat.list` pages asked for through rawClient, oldest first. */
  listCalls: ChatListRequest[];
  /** Urgent flags issued through rawClient, oldest first. */
  urgents: UrgentAppRequest[];
  /** Reactions added with the default addReaction (a configured one is not recorded). */
  reactions: Array<{ messageId: string; emoji: string }>;
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
    renames: [],
    deletes: [],
    listCalls: [],
    urgents: [],
    reactions: [],
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
  // Only the raw-client calls a test configured exist; the rest throw, so a
  // path that reaches the raw client unexpectedly fails loudly.
  const rawV1: Record<string, unknown> = {};
  const chat: Record<string, unknown> = {};
  if (opts.chatUpdate)
    chat.update = async (req: ChatUpdateRequest): Promise<ChatUpdateResult> => {
      fake.renames.push({ chatId: req.path.chat_id, name: req.data.name, description: req.data.description });
      return opts.chatUpdate!(req);
    };
  if (opts.chatDelete)
    chat.delete = async (req: ChatDeleteRequest): Promise<ChatDeleteResult> => {
      fake.deletes.push(req.path.chat_id);
      return opts.chatDelete!(req);
    };
  if (opts.chatList)
    chat.list = async (req: ChatListRequest): Promise<ChatListResult> => {
      fake.listCalls.push(req);
      return opts.chatList!(req);
    };
  if (Object.keys(chat).length) rawV1.chat = chat;
  if (opts.urgentApp)
    rawV1.message = {
      urgentApp: async (req: UrgentAppRequest): Promise<UrgentAppResult> => {
        fake.urgents.push(req);
        return opts.urgentApp!(req);
      },
    };
  const rawClient =
    opts.rawClient ??
    (Object.keys(rawV1).length ? { im: { v1: rawV1 } } : undefined) ??
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
      if (opts.updateCard) await opts.updateCard(messageId, card);
    },
    getChatInfo: opts.getChatInfo ?? notImplemented('getChatInfo'),
    createChat: opts.createChat ?? notImplemented('createChat'),
    addReaction:
      opts.addReaction ??
      (async (messageId: string, emoji: string): Promise<string> => {
        fake.reactions.push({ messageId, emoji });
        return `rid_${fake.reactions.length}`;
      }),
    downloadResourceToFile: opts.downloadResourceToFile ?? notImplemented('downloadResourceToFile'),
    rawClient,
  };
  fake.channel = channel as unknown as ChannelLike;
  return fake;
}
