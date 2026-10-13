import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'

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
  assert.match(source, /reuploadRequest: \(failedMessage\) => activeSocket\.updateMediaMessage\(failedMessage\)/)
  assert.match(source, /proto\.WebMessageInfo\.fromObject\(body\.message\)/)
  assert.match(source, /const refreshedMessage = await activeSocket\.updateMediaMessage\(message\)/)
  assert.match(source, /pathname === '\/media\/retry'/)
  assert.match(source, /maxRetryMediaRequestBytes = 2 \* 1024 \* 1024/)
  assert.match(source, /getContentType\(normalizeMessageContent\(message\.message\)\) !== 'videoMessage'/)
})
