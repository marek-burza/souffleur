/**
 * lib/session.ts
 *
 * The transcript and the answers, kept across a reload.
 */

import type { Answer } from '@/lib/solver'

const TRANSCRIPT_STORAGE = 'souffleur.transcript'
const ANSWERS_STORAGE = 'souffleur.answers'

export function loadTranscript (): string {
  return localStorage.getItem(TRANSCRIPT_STORAGE) ?? ''
}

export function saveTranscript (text: string): void {
  localStorage.setItem(TRANSCRIPT_STORAGE, text)
}

export function loadAnswers (): Answer[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(ANSWERS_STORAGE) ?? '[]')
    return Array.isArray(stored) ? stored : []
  } catch {
    return []
  }
}

export function saveAnswers (answers: Answer[]): void {
  localStorage.setItem(ANSWERS_STORAGE, JSON.stringify(answers))
}
