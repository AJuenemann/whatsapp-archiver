import { createHash, randomUUID } from 'node:crypto'
import { createServer } from 'node:http'
import { createWriteStream } from 'node:fs'
import { chmod, mkdir, rename, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { Transform } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
  getContentType,
  normalizeMessageContent,
  proto,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import pino from 'pino'
import QRCode from 'qrcode'
import { messageMetadata, secureDirectory } from './message-metadata.mjs'

const dataRoot = path.resolve(process.env.DATA_DIR || '/data')
const authRoot = path.join(dataRoot, 'whatsapp-auth')
const eventRoot = path.join(dataRoot, 'whatsapp-events')
const mediaRoot = path.join(dataRoot, 'media', 'whatsapp')
const port = Number(process.env.BRIDGE_PORT || 8765)
const testMode = process.env.WHATSAPP_BRIDGE_MODE === 'mock'
const logger = pino({ level: 'silent' })
const chatNames = new Map()
const knownChatIds = new Set()
const lidToPhone = new Map()
const phoneToLids = new Map()
let authKeys = null
const status = {
  state: testMode ? 'mock' : 'starting',
  paired: false,
  qrDataUrl: null,
  history: { chats: 0, messages: 0, progress: null },
  lastError: null,
}
let reconnectScheduled = false
let resetInProgress = false
let contactsRefreshInProgress = false
let activeSocket = null

await Promise.all([secureDirectory(authRoot), secureDirectory(eventRoot), secureDirectory(mediaRoot)])

async function writeEvent(event) {
  const temporary = path.join(eventRoot, `.${randomUUID()}.part`)
  const destination = path.join(eventRoot, `${Date.now()}-${randomUUID()}.json`)
  await writeFile(temporary, JSON.stringify(event), { mode: 0o600 })
  await rename(temporary, destination)
}

function isRawJid(value) {
  return typeof value === 'string' && /^\d+@(lid|s\.whatsapp\.net|c\.us)$/.test(value)
}

async function saveLidMappings(mappings = []) {
  for (const { lid, pn } of mappings) {
    if (!lid || !pn) continue
    lidToPhone.set(lid, pn)
    const lids = phoneToLids.get(pn) || new Set()
    lids.add(lid)
    phoneToLids.set(pn, lids)
    const name = chatNames.get(pn) || chatNames.get(lid)
    if (name && !isRawJid(name)) {
      chatNames.set(pn, name)
      chatNames.set(lid, name)
      if (knownChatIds.has(pn)) await writeEvent({ type: 'chat', jid: pn, title: name })
      if (knownChatIds.has(lid)) await writeEvent({ type: 'chat', jid: lid, title: name })
    }
  }
}

async function saveContact(contact) {
  const name = contact.name || contact.notify || contact.verifiedName || contact.pushName
  if (!name || isRawJid(name)) return
  const identifiers = new Set([contact.id, contact.lid, contact.phoneNumber].filter(Boolean))
  const updates = new Set()
  for (const id of identifiers) {
    chatNames.set(id, name)
    if (knownChatIds.has(id)) updates.add(id)
    const phone = lidToPhone.get(id)
    if (phone) {
      chatNames.set(phone, name)
      if (knownChatIds.has(phone)) updates.add(phone)
    }
    for (const lid of phoneToLids.get(id) || []) {
      chatNames.set(lid, name)
      if (knownChatIds.has(lid)) updates.add(lid)
    }
  }
  for (const id of updates) await writeEvent({ type: 'chat', jid: id, title: name })
}

function messageText(message) {
  const content = normalizeMessageContent(message?.message)
  if (!content) return ''
  const type = getContentType(content)
  const body = type ? content[type] : undefined
  if (typeof body === 'string') return body
  const labels = {
    imageMessage: 'Bild',
    videoMessage: 'Video',
    audioMessage: 'Audio',
    documentMessage: 'Dokument',
    stickerMessage: 'Sticker',
    albumMessage: 'Medienalbum',
    pollCreationMessageV3: 'Umfrage',
    ptvMessage: 'Video-Nachricht',
    lottieStickerMessage: 'Animierter Sticker',
  }
  const bodyText = body?.text ?? body?.caption ?? body?.question
  if (bodyText) return bodyText
  if (message?.messageStubType != null) {
    const stubTypes = proto.WebMessageInfo.StubType
    const stubType = typeof message.messageStubType === 'number'
      ? Object.keys(stubTypes).find((name) => stubTypes[name] === message.messageStubType)
      : message.messageStubType
    const stubLabels = {
      REVOKE: 'Nachricht gelöscht',
      GROUP_PARTICIPANT_ADD: 'Teilnehmer hinzugefügt',
      GROUP_PARTICIPANT_LEAVE: 'Teilnehmer hat die Gruppe verlassen',
      GROUP_PARTICIPANT_CHANGE_NUMBER: 'Telefonnummer geändert',
      GROUP_PARTICIPANT_REMOVE: 'Teilnehmer entfernt',
      E2E_ENCRYPTED: 'Ende-zu-Ende-Verschlüsselung',
      DISAPPEARING_MODE: 'Ablaufende Nachrichten geändert',
      CHANGE_LIMIT_SHARING: 'Freigabeeinstellungen geändert',
      INDIVIDUAL_CHANGE_NUMBER: 'Telefonnummer geändert',
      GROUP_CHANGE_SUBJECT: 'Gruppenname geändert',
      GROUP_CHANGE_ICON: 'Gruppenbild geändert',
      GROUP_CREATE: 'Gruppe erstellt',
      BIZ_PRIVACY_MODE_INIT_FB: 'Unternehmenseinstellungen geändert',
      GROUP_CHANGE_DESCRIPTION: 'Gruppenbeschreibung geändert',
    }
    return stubLabels[stubType] ?? 'WhatsApp-Systemereignis'
  }
  return labels[type] ?? (type ? `[${type}]` : '')
}

function timestampOf(message) {
  const seconds = Number(message.messageTimestamp?.toString?.() ?? message.messageTimestamp ?? 0)
  return Number.isFinite(seconds) && seconds > 0
    ? new Date(seconds * 1000).toISOString()
    : new Date().toISOString()
}

function safeFileName(value) {
  return path.basename(value || 'attachment').replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 120) || 'attachment'
}

async function saveMedia(message) {
  const content = normalizeMessageContent(message.message)
  const type = getContentType(content)
  if (!['imageMessage', 'videoMessage', 'audioMessage', 'documentMessage', 'stickerMessage'].includes(type)) {
    return null
  }
  const item = content[type]
  const declaredSize = Number(item.fileLength?.toString?.() ?? item.fileLength ?? 0)
  const maxSize = 200 * 1024 * 1024
  if (declaredSize > maxSize) {
    const error = new Error('Media exceeds the 200 MiB archive limit')
    error.name = 'MediaTooLargeError'
    throw error
  }

  const temporary = path.join(mediaRoot, `.${randomUUID()}.part`)
  const digest = createHash('sha256')
  let size = 0
  try {
    const stream = await downloadMediaMessage(message, 'stream', {}, { logger, reuploadRequest: undefined })
    await pipeline(
      stream,
      new Transform({
        transform(chunk, _encoding, callback) {
          size += chunk.length
          if (size > maxSize) {
            const error = new Error('Media exceeds the 200 MiB archive limit')
            error.name = 'MediaTooLargeError'
            callback(error)
            return
          }
          digest.update(chunk)
          callback(null, chunk)
        },
      }),
      createWriteStream(temporary, { flags: 'wx', mode: 0o600 }),
    )
    if (!size) return null
    const sha256 = digest.digest('hex')
    const relative = path.posix.join(
      'whatsapp',
      sha256.slice(0, 2),
      `${sha256}-${safeFileName(item.fileName || `${message.key.id || 'media'}.${type}`)}`,
    )
    const destination = path.join(dataRoot, 'media', relative)
    await mkdir(path.dirname(destination), { recursive: true, mode: 0o700 })
    try {
      await rename(temporary, destination)
    } catch (error) {
      if (error.code !== 'EEXIST') throw error
    }
    return {
      sha256,
      path: relative,
      name: safeFileName(item.fileName || `${message.key.id || 'media'}.${type}`),
      mimeType: item.mimetype || 'application/octet-stream',
    }
  } finally {
    await rm(temporary, { force: true })
  }
}

async function saveChat(chat) {
  if (!chat?.id || chat.id === 'status@broadcast' || chat.id.endsWith('@broadcast')) return
  knownChatIds.add(chat.id)
  const candidate = chat.name || chat.subject || chat.notify || chat.verifiedName || chat.pushName
  const mappedPhone = lidToPhone.get(chat.id)
  const title = candidate && !isRawJid(candidate)
    ? candidate
    : chatNames.get(chat.id) || chatNames.get(mappedPhone) || chat.id
  if (!isRawJid(title)) chatNames.set(chat.id, title)
  const isArchived = typeof chat.archive === 'boolean' ? chat.archive : null
  await writeEvent({ type: 'chat', jid: chat.id, title, isArchived })
  for (const lid of phoneToLids.get(chat.id) || []) {
    chatNames.set(lid, title)
    await writeEvent({ type: 'chat', jid: lid, title, isArchived })
  }
  status.history.chats = chatNames.size
}

async function saveMessage(message) {
  const jid = message.key?.remoteJid || message.key?.remoteJidAlt
  const protocol = message.message?.protocolMessage
  const protocolType = protocol?.type
  const isDelete = protocolType === proto.Message.ProtocolMessage.Type.REVOKE
  const isEdit = protocolType === proto.Message.ProtocolMessage.Type.MESSAGE_EDIT
  if (protocol && !isDelete && !(isEdit && protocol.editedMessage)) return
  const sourceKey = isDelete || isEdit ? protocol.key || message.key : message.key
  const id = sourceKey?.id
  if (!jid || !id || jid === 'status@broadcast' || jid.endsWith('@broadcast')) return
  knownChatIds.add(jid)
  const metadata = messageMetadata(message, jid, sourceKey)
  const title = chatNames.get(jid) || (isRawJid(message.pushName) ? null : message.pushName) || jid
  if (!isRawJid(title)) chatNames.set(jid, title)
  if (isDelete) {
    await writeEvent({
      type: 'delete',
      jid,
      title,
      sourceId: metadata.sourceId,
    })
    return
  }
  if (isEdit && protocol.editedMessage) {
    message = {
      ...message,
      key: { ...message.key, ...sourceKey, remoteJid: jid },
      message: protocol.editedMessage,
    }
  }
  let media = null
  let mediaError = null
  try {
    media = await saveMedia(message)
  } catch (error) {
    mediaError = error?.name || 'MediaDownloadError'
  }
  await writeEvent({
    type: 'message',
    jid,
    title,
    sourceId: metadata.sourceId,
    timestamp: timestampOf(message),
    sender: metadata.sender,
    text: messageText(message),
    media,
    mediaError,
    rawData: message,
  })
  status.history.messages += 1
}

export async function startWhatsApp() {
  const { state, saveCreds } = await useMultiFileAuthState(authRoot)
  authKeys = state.keys
  const socket = makeWASocket({
    auth: state,
    browser: Browsers.macOS('WhatsApp Archive'),
    logger,
    markOnlineOnConnect: false,
    syncFullHistory: true,
  })
  activeSocket = socket

  socket.ev.on('creds.update', saveCreds)
  socket.ev.on('connection.update', async (update) => {
    if (update.qr) {
      status.state = 'pairing_required'
      status.qrDataUrl = await QRCode.toDataURL(update.qr, { margin: 1, width: 280 })
      status.lastError = null
    }
    if (update.connection === 'open') {
      status.state = 'connected'
      status.paired = true
      status.qrDataUrl = null
      status.lastError = null
      reconnectScheduled = false
    }
    if (update.connection === 'close') {
      const code = update.lastDisconnect?.error?.output?.statusCode
      status.qrDataUrl = null
      if (code === DisconnectReason.loggedOut) {
        status.state = 'logged_out'
        status.paired = false
        status.lastError = 'WhatsApp hat die verknüpfte Sitzung abgemeldet.'
      } else if (!reconnectScheduled) {
        status.state = 'reconnecting'
        reconnectScheduled = true
        setTimeout(() => startWhatsApp().catch((error) => {
          status.state = 'error'
          status.lastError = error?.name || 'ConnectionError'
          reconnectScheduled = false
        }), 2000).unref()
      }
    }
  })

  socket.ev.on('messaging-history.set', async ({ chats = [], contacts = [], messages = [], lidPnMappings = [], progress, isLatest }) => {
    status.state = isLatest ? 'connected' : 'syncing_history'
    status.history.progress = progress ?? null
    await saveLidMappings(lidPnMappings)
    for (const contact of contacts) await saveContact(contact)
    for (const chat of chats) await saveChat(chat)
    for (const message of messages) await saveMessage(message)
  })

  socket.ev.on('chats.upsert', async (chats) => {
    for (const chat of chats) await saveChat(chat)
  })

  socket.ev.on('chats.update', async (updates) => {
    for (const update of updates) {
      const archive = typeof update.archive === 'boolean' ? update.archive : update.archived
      if (typeof archive !== 'boolean') continue
      await saveChat({ id: update.id, name: chatNames.get(update.id), archive })
    }
  })

  socket.ev.on('contacts.upsert', async (contacts) => {
    for (const contact of contacts) await saveContact(contact)
  })

  socket.ev.on('contacts.update', async (contacts) => {
    for (const contact of contacts) await saveContact(contact)
  })

  socket.ev.on('lid-mapping.update', async (mapping) => {
    await saveLidMappings([mapping])
  })

  socket.ev.on('messages.upsert', async ({ messages = [], type }) => {
    if (type !== 'notify' && type !== 'append') return
    for (const message of messages) await saveMessage(message)
  })
}

async function resetLocalPairing() {
  if (status.state !== 'logged_out') {
    const error = new Error('Pairing can only be reset after logout')
    error.statusCode = 409
    throw error
  }
  if (resetInProgress) {
    const error = new Error('Pairing reset is already in progress')
    error.statusCode = 409
    throw error
  }

  resetInProgress = true
  try {
    await rm(authRoot, { recursive: true, force: true })
    await secureDirectory(authRoot)
    status.state = 'starting'
    status.paired = false
    status.qrDataUrl = null
    status.lastError = null
    status.history = { chats: 0, messages: 0, progress: null }
    reconnectScheduled = false
    await startWhatsApp()
    return status
  } finally {
    resetInProgress = false
  }
}

async function resolveLidPhones(jids) {
  const lidUsers = [...new Set(jids.filter((jid) => /^\d+@lid$/.test(jid)).map((jid) => jid.slice(0, -4)))]
  if (!authKeys || lidUsers.length === 0) return {}
  const reverseKeys = lidUsers.map((user) => `${user}_reverse`)
  const stored = await authKeys.get('lid-mapping', reverseKeys)
  const resolved = {}
  for (let index = 0; index < lidUsers.length; index++) {
    const phoneUser = stored[reverseKeys[index]]
    if (/^\d+$/.test(phoneUser || '')) resolved[`${lidUsers[index]}@lid`] = `${phoneUser}@s.whatsapp.net`
  }
  return resolved
}

async function refreshContacts() {
  if (!activeSocket || !authKeys || !status.paired || status.state === 'syncing_history') {
    const error = new Error('Contacts can only be refreshed while connected and idle')
    error.statusCode = 409
    throw error
  }
  if (contactsRefreshInProgress) {
    const error = new Error('Contact refresh is already in progress')
    error.statusCode = 409
    throw error
  }
  contactsRefreshInProgress = true
  try {
    await authKeys.set({ 'app-state-sync-version': { regular_low: null } })
    await activeSocket.resyncAppState(['regular_low'], true)
    await activeSocket.resyncAppState(['regular'], false)
    const versions = await authKeys.get('app-state-sync-version', ['regular_low'])
    if (!versions.regular_low) {
      const error = new Error('WhatsApp did not return the chat app-state snapshot')
      error.statusCode = 503
      throw error
    }
    return { state: 'contacts_refreshed' }
  } finally {
    contactsRefreshInProgress = false
  }
}

const server = createServer(async (request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname
  if (request.method === 'POST' && pathname === '/contacts/refresh') {
    refreshContacts().then((result) => {
      response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' })
      response.end(JSON.stringify(result))
    }).catch((error) => {
      response.writeHead(error.statusCode || 500, { 'content-type': 'application/json', 'cache-control': 'no-store' })
      response.end(JSON.stringify({ error: error.statusCode ? error.message : 'Contact refresh failed' }))
    })
    return
  }
  if (request.method === 'POST' && pathname === '/resolve-lids') {
    try {
      const chunks = []
      let size = 0
      for await (const chunk of request) {
        size += chunk.length
        if (size > 32768) {
          response.writeHead(413).end()
          return
        }
        chunks.push(chunk)
      }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      if (!Array.isArray(body.jids) || body.jids.length > 200 || body.jids.some((jid) => typeof jid !== 'string')) {
        response.writeHead(400).end()
        return
      }
      const result = await resolveLidPhones(body.jids)
      response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' })
      response.end(JSON.stringify(result))
    } catch {
      response.writeHead(400).end()
    }
    return
  }
  if (request.method === 'POST' && pathname === '/pairing/reset') {
    resetLocalPairing().then((nextStatus) => {
      response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' })
      response.end(JSON.stringify(nextStatus))
    }).catch((error) => {
      response.writeHead(error.statusCode || 500, { 'content-type': 'application/json', 'cache-control': 'no-store' })
      response.end(JSON.stringify({ error: error.statusCode ? error.message : 'Pairing reset failed' }))
    })
    return
  }
  if (request.method !== 'GET' || !['/health', '/status'].includes(pathname)) {
    response.writeHead(404).end()
    return
  }
  response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' })
  response.end(JSON.stringify(request.url.startsWith('/health') ? { status: 'ok' } : status))
})

server.listen(port, '0.0.0.0')
if (!testMode && process.env.WHATSAPP_BRIDGE_TEST_MODE !== '1') {
  startWhatsApp().catch((error) => {
    status.state = 'error'
    status.lastError = error?.name || 'ConnectionError'
  })
}

process.on('SIGTERM', () => server.close(() => process.exit(0)))
process.on('SIGINT', () => server.close(() => process.exit(0)))