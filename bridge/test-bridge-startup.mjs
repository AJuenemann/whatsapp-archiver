import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import { aesEncryptGCM, hkdf, proto } from '@whiskeysockets/baileys'
import { archivedMediaMessage, downloadWithReupload, referencedMediaMessage, reuploadMedia } from './media-download.mjs'

test('bridge boot starts the WhatsApp socket', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(
    source,
    /server\.listen\([^)]*\)[\s\S]*if\s*\(\s*!testMode\s*&&\s*process\.env\.WHATSAPP_BRIDGE_TEST_MODE\s*!==\s*'1'\s*\)\s*\{[\s\S]*startWhatsApp\s*\(/,
  )
})

test('local pairing reset is POST-only and guarded by logged-out state', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  const logoutGuard = source.indexOf("if (status.state !== 'logged_out')")
  const authRemoval = source.indexOf('await rm(authRoot', logoutGuard)

  assert.notEqual(logoutGuard, -1)
  assert.ok(authRemoval > logoutGuard)
  assert.match(source, /request\.method === 'POST' && pathname === '\/pairing\/reset'/)
  assert.match(source, /await startWhatsApp\(\)[\s\S]*return status/)
})

test('bridge preserves plain conversation text and skips other protocol messages', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(source, /if \(typeof body === 'string'\) return body/)
  assert.match(source, /if \(protocol && !isDelete && !\(isEdit && protocol\.editedMessage\)\) return/)
  assert.match(source, /WhatsApp-Systemereignis/)
})

test('contact updates accept Baileys display-name alternatives', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(source, /contact\.name \|\| contact\.notify \|\| contact\.username \|\| contact\.verifiedName \|\| contact\.pushName/)
  assert.match(source, /chat\.notify \|\| chat\.username \|\| chat\.verifiedName/)
  assert.match(source, /if \(!isRawJid\(title\)\) chatNames\.set\(chat\.id, title\)/)
  assert.match(source, /lidPnMappings = \[\]/)
  assert.match(source, /await saveLidMappings\(lidPnMappings\)/)
  assert.match(source, /socket\.ev\.on\('lid-mapping\.update'/)
  assert.match(source, /if \(knownChatIds\.has\(id\)\) updates\.add\(id\)/)
})

test('LID resolution reads only stored reverse mappings in bounded batches', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(source, /authKeys\.get\('lid-mapping', reverseKeys\)/)
  assert.match(source, /pathname === '\/resolve-lids'/)
  assert.match(source, /body\.jids\.length > 200/)
  assert.match(source, /response\.writeHead\(413\)/)
})

test('stored videos can be retried through a bounded reupload-aware endpoint', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  assert.match(source, /await downloadWithReupload\(/)
  assert.match(source, /proto\.WebMessageInfo\.fromObject\(body\.message\)/)
  assert.match(source, /const media = await saveMedia\(candidate\)/)
  assert.match(source, /pathname === '\/media\/retry'/)
  assert.match(source, /maxRetryMediaRequestBytes = 2 \* 1024 \* 1024/)
  assert.match(source, /'imageMessage', 'videoMessage', 'audioMessage', 'documentMessage', 'stickerMessage'/)
})

test('retry downloads first and preserves archive identity without requiring a phone reupload', async () => {
  const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8')
  const retry = source.slice(source.indexOf('async function retryVideo('), source.indexOf('\nconst server ='))
  const events = []
  let downloads = 0
  const socket = { updateMediaMessage() { assert.fail('A usable media URL must not require a phone reupload') } }
  const factory = new Function(
    'activeSocket', 'status', 'normalizeMessageContent', 'getContentType', 'saveMedia',
    'messageMetadata', 'writeEvent', 'chatNames', 'timestampOf', 'messageText',
    'archivedMediaMessage',
    'const mediaRetriesInProgress = new Set();' + retry + '; return retryVideo;',
  )
  const run = factory(
    socket, { paired: true }, content => content, content => Object.keys(content)[0],
    async () => { downloads++; return { path: 'saved.bin', sha256: 'a'.repeat(64) } },
    (_message, _jid, key) => ({ sourceId: key.id + ':in:', sender: 'Person' }),
    async event => events.push(event), new Map(), () => '2026-01-01T00:00:00Z', () => 'Saved text',
    archivedMediaMessage,
  )
  for (const kind of ['imageMessage', 'videoMessage', 'audioMessage', 'documentMessage', 'stickerMessage']) {
    await run({ key: { remoteJid: 'group@g.us', id: kind }, message: { [kind]: {} } })
  }
  assert.equal(downloads, 5)
  assert.equal(events.length, 5)
  assert.ok(events.every(event => event.historical && event.media))
  assert.equal(events[0].sourceId, 'imageMessage:in:')
})

test('media normalization includes video notes and album/animated-sticker wrappers', () => {
  for (const wrapper of ['associatedChildMessage', 'lottieStickerMessage', 'documentWithCaptionMessage',
    'viewOnceMessageV2Extension']) {
    const original = { key: { id: 'saved' }, message: { [wrapper]: {
      message: { ptvMessage: { mimetype: 'video/mp4' } },
    } } }
    const normalized = archivedMediaMessage(original)
    assert.equal(normalized.message.videoMessage.mimetype, 'video/mp4')
    assert.ok(original.message[wrapper].message.ptvMessage)
    assert.equal(normalized.key, original.key)
  }
})

test('quoted files use their original identity but do not change the owning archive message', () => {
  const quote = { url: 'https://example.invalid', mediaKey: Buffer.alloc(32) }
  const original = {
    key: { id: 'reply', remoteJid: 'group@g.us', fromMe: true },
    message: { extendedTextMessage: { text: 'reply', contextInfo: {
      stanzaId: 'quoted', participant: '123@lid',
      quotedMessage: { imageMessage: quote },
    } } },
  }
  const socket = { authState: { creds: { me: { id: '456:1@s.whatsapp.net', lid: '789@lid' } } } }
  const selected = referencedMediaMessage(
    original, 'message.extendedTextMessage.contextInfo.quotedMessage.imageMessage', socket,
  )
  assert.deepEqual(selected.key, {
    id: 'quoted', remoteJid: 'group@g.us', fromMe: false, participant: '123@lid',
  })
  assert.equal(selected.message.imageMessage, quote)
  assert.equal(original.key.id, 'reply')
  original.message.extendedTextMessage.contextInfo.participant = '789@lid'
  assert.equal(referencedMediaMessage(
    original, 'message.extendedTextMessage.contextInfo.quotedMessage.imageMessage', socket,
  ).key.fromMe, true)
  assert.throws(() => referencedMediaMessage(original, 'message.__proto__.imageMessage', socket))
  assert.throws(() => referencedMediaMessage(original, 'message.extendedTextMessage', socket))
})

test('referenced favicon is downloaded as a link thumbnail', () => {
  const favicon = { thumbnailDirectPath: '/favicon', mediaKey: Buffer.alloc(32) }
  const message = { key: {}, message: { extendedTextMessage: { faviconMMSMetadata: favicon } } }
  const candidate = referencedMediaMessage(
    message, 'message.extendedTextMessage.faviconMMSMetadata', {},
  )
  assert.equal(candidate.message.extendedTextMessage, favicon)
})

test('reupload includes history group participant without mutating saved identity', async () => {
  let requested
  const original = { key: { id: 'saved', remoteJid: 'group@g.us' }, participant: '123@lid' }
  let attempts = 0
  const result = await downloadWithReupload(original, async () => {
    if (++attempts === 1) throw { output: { statusCode: 410 } }
    return 'downloaded'
  }, async message => { requested = message; return message })
  assert.equal(result, 'downloaded')
  assert.equal(attempts, 2)
  assert.equal(requested.key.participant, '123@lid')
  assert.equal(original.key.participant, undefined)
  assert.equal(requested.key.id, original.key.id)
})

test('download errors use Boom status codes and never retry unrelated failures', async () => {
  for (const status of [403, 404, 410, 429, 500]) {
    let downloads = 0
    let uploads = 0
    const error = { output: { statusCode: status } }
    const promise = downloadWithReupload({ key: {} }, async () => {
      if (++downloads === 1) throw error
      return 'media'
    }, async message => { uploads++; return message })
    if ([403, 404, 410].includes(status)) {
      assert.equal(await promise, 'media')
      assert.equal(uploads, 1)
    } else {
      await assert.rejects(promise, value => value === error)
      assert.equal(uploads, 0)
    }
  }
})

test('an unusable refreshed URL reports download stage without repeating reuploads', async () => {
  let uploads = 0
  await assert.rejects(downloadWithReupload({ key: {} }, async () => {
    throw { output: { statusCode: 404 } }
  }, async message => { uploads++; return message }), error =>
    error.name === 'MediaDownloadAfterReuploadError' &&
    error.message.includes('HTTP 404') && error.statusCode === 503)
  assert.equal(uploads, 1)
})

function reuploadFixture(send) {
  const ev = new EventEmitter()
  const message = {
    key: { id: 'saved-video', remoteJid: '123@lid', fromMe: false },
    message: { videoMessage: { mediaKey: Buffer.alloc(32, 1), directPath: '/old' } },
  }
  const socket = {
    ev,
    authState: { creds: { me: { id: '456:1@s.whatsapp.net' } } },
    getMediaHost: () => 'mmg.whatsapp.net',
    sendNode: request => send(request, ev, message),
  }
  return { ev, message, socket }
}

function encryptedMediaUpdate(message, result, directPath) {
  const mediaKey = message.message.videoMessage.mediaKey
  const iv = Buffer.alloc(12, 2)
  const key = hkdf(mediaKey, 32, { info: 'WhatsApp Media Retry Notification' })
  const payload = proto.MediaRetryNotification.encode({ result, directPath }).finish()
  return {
    key: message.key,
    media: { iv, ciphertext: aesEncryptGCM(payload, key, iv, Buffer.from(message.key.id)) },
  }
}

function assertReuploadListenersRemoved(ev) {
  assert.equal(ev.listenerCount('messages.media-update'), 0)
  assert.equal(ev.listenerCount('connection.update'), 0)
}

test('phone reupload times out, cleans listeners and permits a subsequent attempt', async () => {
  const { ev, message, socket } = reuploadFixture(() => {})
  await assert.rejects(reuploadMedia(message, socket, 10), error => error.statusCode === 504)
  assertReuploadListenersRemoved(ev)
  socket.sendNode = () => ev.emit('messages.media-update', [
    encryptedMediaUpdate(message, proto.MediaRetryNotification.ResultType.SUCCESS, '/recovered'),
  ])
  assert.equal(await reuploadMedia(message, socket), message)
  assert.equal(message.message.videoMessage.directPath, '/recovered')
  assert.equal(message.message.videoMessage.url, 'https://mmg.whatsapp.net/recovered')
  assertReuploadListenersRemoved(ev)
})

test('phone reupload installs listeners before sending and ignores unrelated replies', async () => {
  const { ev, message, socket } = reuploadFixture((request, events, candidate) => {
    assert.equal(request.attrs.type, 'server-error')
    assert.equal(request.content[1].attrs.jid, candidate.key.remoteJid)
    events.emit('messages.media-update', [{ key: { id: 'unrelated' }, error: new Error('Unrelated') }])
    events.emit('messages.media-update', [
      encryptedMediaUpdate(candidate, proto.MediaRetryNotification.ResultType.SUCCESS, '/new'),
    ])
  })
  await reuploadMedia(message, socket)
  assert.equal(message.message.videoMessage.directPath, '/new')
  assertReuploadListenersRemoved(ev)
})

test('phone reupload cleans listeners on send errors, disconnects and device failures', async () => {
  const sendError = new Error('send failed')
  for (const mode of ['send', 'disconnect', 'device', 'not-found', 'missing-path', 'decrypt']) {
    const { ev, message, socket } = reuploadFixture((_request, events, candidate) => {
      if (mode === 'send') throw sendError
      if (mode === 'disconnect') {
        events.emit('connection.update', { connection: 'close' })
      } else if (mode === 'device') {
        events.emit('messages.media-update', [{ key: candidate.key, error: sendError }])
      } else if (mode === 'decrypt') {
        const update = encryptedMediaUpdate(candidate, proto.MediaRetryNotification.ResultType.SUCCESS, '/new')
        update.media.ciphertext[0] ^= 1
        events.emit('messages.media-update', [update])
      } else {
        events.emit('messages.media-update', [encryptedMediaUpdate(
          candidate,
          mode === 'not-found' ? proto.MediaRetryNotification.ResultType.NOT_FOUND :
            proto.MediaRetryNotification.ResultType.SUCCESS,
        )])
      }
    })
    await assert.rejects(reuploadMedia(message, socket))
    assert.equal(message.message.videoMessage.directPath, '/old')
    assertReuploadListenersRemoved(ev)
  }
})
