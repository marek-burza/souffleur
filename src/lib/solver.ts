/**
 * lib/solver.ts
 *
 * The solver. The provider is inferred from the key prefix, so there is no
 * provider toggle to keep in sync with the key. The API is called straight from
 * the browser, so the key lives in LocalStorage and is visible to anyone with
 * the device - acceptable only because this is a single-user tool.
 */

import type { BaseChatModel } from '@langchain/core/language_models/chat_models'
import type { ContentBlock } from '@langchain/core/messages'
import { ChatAnthropic } from '@langchain/anthropic'
import { HumanMessage } from '@langchain/core/messages'
import { ChatOpenAI } from '@langchain/openai'

export type Provider = 'anthropic' | 'openai'

const ANTHROPIC_KEY_PREFIX = 'sk-ant-'

const SCREENSHOT_MEDIA_TYPE = 'image/jpeg'

// Thinking tokens are drawn from the same budget as the answer, so the solver
// needs headroom - without it a long deliberation eats the budget and the
// answer is truncated mid-sentence. Both providers count reasoning against this
// same limit (`max_tokens` for Anthropic, `max_completion_tokens` for OpenAI).
const SOLVE_MAX_TOKENS = 8192

// The solver spots the question and answers it in one request, so it carries
// the whole live path on its own. Reasoning at low effort buys answer quality
// without the turnaround a higher effort would add. Both providers accept the
// same four words for this, so one constant covers both.
const SOLVE_EFFORT = 'low'

export const PROVIDER_TITLES: Record<Provider, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
}

export const MODELS: Record<Provider, { title: string, value: string }[]> = {
  anthropic: [
    { title: 'Sonnet 5 (faster)', value: 'claude-sonnet-5' },
    { title: 'Opus 5 (thorough, slower)', value: 'claude-opus-5' },
  ],
  openai: [
    { title: 'GPT-5.6 Terra (faster)', value: 'gpt-5.6-terra' },
    { title: 'GPT-5.6 Sol (thorough, slower)', value: 'gpt-5.6-sol' },
  ],
}

export interface Answer {
  question: string
  text: string
}

export function providerOf (apiKey: string): Provider {
  if (!apiKey || apiKey.startsWith(ANTHROPIC_KEY_PREFIX)) {
    return 'anthropic'
  }
  return 'openai'
}

export function resolveModel (apiKey: string, model: string): string {
  const models = MODELS[providerOf(apiKey)]
  return models.some(candidate => candidate.value === model) ? model : models[0].value
}

export function createModel (apiKey: string, model: string): BaseChatModel {
  switch (providerOf(apiKey)) {
    case 'anthropic': {
      return new ChatAnthropic({
        apiKey,
        model,
        maxTokens: SOLVE_MAX_TOKENS,
        thinking: { type: 'adaptive' },
        outputConfig: { effort: SOLVE_EFFORT },
        clientOptions: { dangerouslyAllowBrowser: true },
      })
    }
    case 'openai': {
      return new ChatOpenAI({
        apiKey,
        model,
        maxTokens: SOLVE_MAX_TOKENS,
        reasoning: { effort: SOLVE_EFFORT },
        configuration: { dangerouslyAllowBrowser: true },
      })
    }
  }
}

export function splitQuestion (text: string): { question: string, answer: string } {
  const lines = text.split('\n')
  const index = lines.findIndex(line => line.startsWith('QUESTION:'))
  if (index === -1) {
    return { question: '', answer: text }
  }
  return {
    question: lines[index].slice('QUESTION:'.length).trim(),
    answer: lines.slice(index + 1).join('\n').trim(),
  }
}

export async function solve (
  model: BaseChatModel,
  prompt: string,
  transcript: string,
  screenshot: string,
): Promise<Answer> {
  const content: ContentBlock[] = []
  if (screenshot) {
    content.push({
      type: 'image_url',
      image_url: { url: `data:${SCREENSHOT_MEDIA_TYPE};base64,${screenshot}` },
    })
  }
  content.push({
    type: 'text',
    text: prompt.replace('{transcript}', transcript || '(empty)'),
  })

  const response = await model.invoke([new HumanMessage({ content })])

  const { question, answer } = splitQuestion(response.text.trim())
  return { question, text: answer }
}
