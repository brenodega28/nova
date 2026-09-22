/**
 * Below are the colors that are used in the app. The colors are defined in the light and dark mode.
 * There are many other ways to style your app. For example, [Nativewind](https://www.nativewind.dev/), [Tamagui](https://tamagui.dev/), [unistyles](https://reactnativeunistyles.vercel.app), etc.
 */

import '@/global.css';

import { Platform } from 'react-native';

export const Colors = {
  light: {
    text: '#16213E',
    background: '#F3F7FD',
    backgroundElement: '#FFFFFF',
    backgroundSelected: '#E7EEFE',
    textSecondary: '#5C6B8C',
    accent: '#3B6EF0',
    accentSurface: '#E7EEFE',
    onAccent: '#FFFFFF',
    border: '#DCE3F5',
    chevron: '#8892B0',
  },
  dark: {
    text: '#E8EDF9',
    background: '#0B1020',
    backgroundElement: '#151C31',
    backgroundSelected: '#22304F',
    textSecondary: '#A3AFCD',
    accent: '#7FA6F7',
    accentSurface: '#22304F',
    onAccent: '#0B1020',
    border: '#2A3455',
    chevron: '#7C88A8',
  },
} as const;

export type ThemeColor = keyof typeof Colors.light & keyof typeof Colors.dark;

export const Fonts = Platform.select({
  ios: {
    /** iOS `UIFontDescriptorSystemDesignDefault` */
    sans: 'system-ui',
    /** iOS `UIFontDescriptorSystemDesignSerif` */
    serif: 'ui-serif',
    /** iOS `UIFontDescriptorSystemDesignRounded` */
    rounded: 'ui-rounded',
    /** iOS `UIFontDescriptorSystemDesignMonospaced` */
    mono: 'ui-monospace',
  },
  default: {
    sans: 'normal',
    serif: 'serif',
    rounded: 'normal',
    mono: 'monospace',
  },
  web: {
    sans: 'var(--font-display)',
    serif: 'var(--font-serif)',
    rounded: 'var(--font-rounded)',
    mono: 'var(--font-mono)',
  },
});

export const FontFamily = {
  displaySemiBold: 'Sora_600SemiBold',
  displayBold: 'Sora_700Bold',
  body: 'Manrope_400Regular',
  bodyMedium: 'Manrope_500Medium',
  bodySemiBold: 'Manrope_600SemiBold',
  bodyBold: 'Manrope_700Bold',
} as const;

export const Spacing = {
  half: 2,
  one: 4,
  two: 8,
  three: 16,
  four: 24,
  five: 32,
  six: 64,
} as const;

export const Radius = {
  icon: 13,
  card: 18,
  avatar: 30,
} as const;

export const HorizontalPadding = '5%';

export const BottomTabInset = Platform.select({ ios: 50, android: 80 }) ?? 0;
export const MaxContentWidth = 800;
