import { chmod, mkdir, stat } from 'node:fs/promises'

export function messageChatName(message, jid) {
  if (message.key?.fromMe || jid.endsWith('@g.us')) return null
  const name = message.pushName
  return typeof name === 'string' && name && !/^\d+@(lid|s\.whatsapp\.net|c\.us)$/.test(name)
    ? name : null
}

export function messageMetadata(message, jid, sourceKey) {
  const participant = sourceKey.participant || sourceKey.participantAlt || ''
  const senderParticipant = participant || message.participant || ''
  const fromMe = Boolean(sourceKey.fromMe)
  return {
    sourceId: `${sourceKey.id}:${fromMe ? 'out' : 'in'}:${participant}`,
    sender: fromMe ? 'You' : message.pushName || senderParticipant || jid,
  }
}

export async function secureDirectory(directory) {
  await mkdir(directory, { recursive: true, mode: 0o700 })
  await chmod(directory, 0o700)
  const details = await stat(directory)
  if ((details.mode & 0o777) !== 0o700) throw new Error('Could not secure the WhatsApp session directory')
}