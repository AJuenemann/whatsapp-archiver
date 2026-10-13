import {
  assertMediaContent,
  decryptMediaRetryData,
  encryptMediaRetryRequest,
  getUrlFromDirectPath,
  jidNormalizedUser,
  normalizeMessageContent,
  proto,
} from '@whiskeysockets/baileys'

export function referencedMediaMessage(message, part, socket) {
  if (!part) return archivedMediaMessage(message)
  const keys = part.split('.')
  const kind = keys.at(-1)
  if (keys[0] !== 'message' || keys.length > 34 ||
      !['imageMessage', 'videoMessage', 'audioMessage', 'documentMessage', 'stickerMessage',
        'ptvMessage', 'extendedTextMessage', 'faviconMMSMetadata'].includes(kind)) {
    throw new Error('Invalid archived media reference')
  }
  let item = message
  let quoteContext
  for (const key of keys) {
    if (key === 'quotedMessage') quoteContext = item
    if (!item || typeof item !== 'object' || !Object.hasOwn(item, key)) {
      throw new Error('Archived media reference does not exist')
    }
    item = item[key]
  }
  if (!item?.mediaKey || !(item.url || item.directPath || item.thumbnailDirectPath)) {
    throw new Error('Archived media reference has no download metadata')
  }
  let key = message.key
  if (quoteContext) {
    if (!quoteContext.stanzaId || !quoteContext.participant) {
      throw new Error('Quoted media has no original message identity')
    }
    const me = socket.authState.creds.me
    const sender = jidNormalizedUser(quoteContext.participant)
    key = {
      id: quoteContext.stanzaId,
      remoteJid: quoteContext.remoteJid || key.remoteJid,
      fromMe: [me.id, me.lid].filter(Boolean).some(jid => jidNormalizedUser(jid) === sender),
    }
    if (key.remoteJid.endsWith('@g.us')) key.participant = quoteContext.participant
  }
  const mediaKind = kind === 'ptvMessage' ? 'videoMessage' :
    kind === 'faviconMMSMetadata' ? 'extendedTextMessage' : kind
  return { ...message, key, message: { [mediaKind]: item } }
}

export function archivedMediaMessage(message) {
  let content = message.message
  const wrappers = ['ephemeralMessage', 'viewOnceMessage', 'viewOnceMessageV2',
    'viewOnceMessageV2Extension', 'documentWithCaptionMessage', 'associatedChildMessage',
    'lottieStickerMessage']
  for (let depth = 0; depth < 8; depth++) {
    if (!content || typeof content !== 'object') return { ...message, message: undefined }
    const wrapper = wrappers.find(key => content[key]?.message)
    if (!wrapper) {
      content = normalizeMessageContent(content)
      if (content?.ptvMessage) content = { videoMessage: content.ptvMessage }
      return { ...message, message: content }
    }
    content = content[wrapper].message
  }
  throw new Error('Media message nesting exceeds the supported limit')
}

export async function reuploadMedia(message, socket, timeoutMs = 60000) {
  const content = message.message?.extendedTextMessage || assertMediaContent(message.message)
  const request = encryptMediaRetryRequest(message.key, content.mediaKey, socket.authState.creds.me.id)
  const result = await new Promise((resolve, reject) => {
    const finish = (error, update) => {
      clearTimeout(timer)
      socket.ev.off('messages.media-update', onMediaUpdate)
      socket.ev.off('connection.update', onConnectionUpdate)
      if (error) reject(error)
      else resolve(update)
    }
    const onMediaUpdate = updates => {
      const update = updates.find(candidate => candidate.key.id === message.key.id)
      if (!update) return
      if (update.error) {
        const status = update.error.output?.statusCode ?? update.error.status
        const error = new Error(
          `Hauptgerät hat die Medienanforderung abgelehnt${Number.isInteger(status) ? ` (HTTP ${status})` : ''}`,
          { cause: update.error },
        )
        error.statusCode = 503
        error.name = 'MediaReuploadResponseError'
        finish(error)
      } else {
        finish(undefined, update)
      }
    }
    const onConnectionUpdate = update => {
      if (update.connection !== 'close') return
      const error = new Error('WhatsApp-Verbindung beim erneuten Bereitstellen unterbrochen')
      error.statusCode = 503
      finish(error)
    }
    const timer = setTimeout(() => {
      const error = new Error('Keine Antwort auf die Medienanforderung vom Hauptgerät innerhalb von 60 Sekunden')
      error.statusCode = 504
      finish(error)
    }, timeoutMs)
    socket.ev.on('messages.media-update', onMediaUpdate)
    socket.ev.on('connection.update', onConnectionUpdate)
    // Baileys rc14's updateMediaMessage waits indefinitely and retains its listeners.
    Promise.resolve().then(() => socket.sendNode(request)).catch(error => finish(error))
  })
  const media = decryptMediaRetryData(result.media, content.mediaKey, message.key.id)
  if (media.result !== proto.MediaRetryNotification.ResultType.SUCCESS) {
    const reason = proto.MediaRetryNotification.ResultType[media.result] || 'UNKNOWN'
    const error = new Error(`Hauptgerät konnte das Medium nicht bereitstellen (${reason})`)
    error.statusCode = 503
    throw error
  }
  if (!media.directPath) {
    const error = new Error('WhatsApp lieferte keine Downloadadresse für das Medium')
    error.statusCode = 503
    throw error
  }
  if (message.message?.extendedTextMessage) {
    content.thumbnailDirectPath = media.directPath
  } else {
    content.directPath = media.directPath
    content.url = getUrlFromDirectPath(media.directPath, socket.getMediaHost())
  }
  return message
}

export async function downloadWithReupload(message, download, reupload) {
  try {
    return await download(message)
  } catch (error) {
    const status = error?.output?.statusCode ?? error?.status
    if (![403, 404, 410].includes(status)) throw error
    const updated = await reupload({
      ...message,
      key: {
        ...message.key,
        participant: message.key.participant || message.participant || undefined,
      },
    })
    try {
      return await download(updated)
    } catch (cause) {
      const status = cause?.output?.statusCode ?? cause?.status
      const failure = new Error(
        `Download nach erneuter Bereitstellung fehlgeschlagen${Number.isInteger(status) ? ` (HTTP ${status})` : ''}`,
        { cause },
      )
      failure.statusCode = 503
      failure.name = 'MediaDownloadAfterReuploadError'
      throw failure
    }
  }
}
