/**
 * lib/settings.ts
 *
 * The API key (Anthropic or OpenAI), the solving model, and the solve prompt.
 */

import { resolvePrompt } from '@/lib/prompt'
import { resolveModel } from '@/lib/solver'

const API_KEY_STORAGE = 'souffleur.apiKey'
const MODEL_STORAGE = 'souffleur.model'
const PROMPT_STORAGE = 'souffleur.prompt'

export interface Settings {
  apiKey: string
  model: string
  prompt: string
}

export function loadSettings (): Settings {
  const apiKey = localStorage.getItem(API_KEY_STORAGE) ?? ''
  return {
    apiKey,
    model: resolveModel(apiKey, localStorage.getItem(MODEL_STORAGE) ?? ''),
    prompt: resolvePrompt(localStorage.getItem(PROMPT_STORAGE) ?? '').value,
  }
}

export function saveSettings (settings: Partial<Settings>): void {
  if (settings.apiKey !== undefined) {
    localStorage.setItem(API_KEY_STORAGE, settings.apiKey)
  }
  if (settings.model !== undefined) {
    localStorage.setItem(MODEL_STORAGE, settings.model)
  }
  if (settings.prompt !== undefined) {
    localStorage.setItem(PROMPT_STORAGE, settings.prompt)
  }
}
