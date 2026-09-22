export const AssistantStates = ['waking', 'listening', 'thinking', 'idle'] as const;

export type AssistantState = (typeof AssistantStates)[number];

export const AssistantStateCopy: Record<AssistantState, { label: string; caption: string }> = {
  waking: { label: 'Waking up', caption: 'Getting ready to listen' },
  listening: { label: 'Listening…', caption: "Go ahead, I'm listening" },
  thinking: { label: 'Thinking…', caption: 'Working through your request' },
  idle: { label: 'Idle', caption: 'Say the wake word to start' },
};

export type ConfigIcon = 'microphone' | 'chip' | 'waveform' | 'sparkle';

export type ConfigEntry = {
  key: string;
  icon: ConfigIcon;
  title: string;
  value: string;
};

export const MockedAssistantState: AssistantState = 'listening';

export const MockedConfiguration: ConfigEntry[] = [
  { key: 'voice', icon: 'microphone', title: 'Voice', value: 'en_US-ryan-medium' },
  { key: 'llm', icon: 'chip', title: 'Language model', value: 'qwen3:14b · localhost:11434' },
  { key: 'wake', icon: 'waveform', title: 'Wake model', value: 'base.en · mps' },
  { key: 'question', icon: 'sparkle', title: 'Question model', value: 'large · mps' },
];
