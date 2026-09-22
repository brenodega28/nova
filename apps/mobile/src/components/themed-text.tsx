import { Platform, StyleSheet, Text, type TextProps } from 'react-native';

import { FontFamily, Fonts, ThemeColor } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

export type ThemedTextProps = TextProps & {
  type?:
    | 'default'
    | 'title'
    | 'small'
    | 'smallBold'
    | 'subtitle'
    | 'link'
    | 'linkPrimary'
    | 'code'
    | 'wordmark'
    | 'sectionLabel'
    | 'stateLabel'
    | 'rowTitle'
    | 'rowValue';
  themeColor?: ThemeColor;
};

export function ThemedText({ style, type = 'default', themeColor, ...rest }: ThemedTextProps) {
  const theme = useTheme();

  return (
    <Text
      style={[
        { color: theme[themeColor ?? 'text'] },
        type === 'default' && styles.default,
        type === 'title' && styles.title,
        type === 'small' && styles.small,
        type === 'smallBold' && styles.smallBold,
        type === 'subtitle' && styles.subtitle,
        type === 'link' && styles.link,
        type === 'linkPrimary' && styles.linkPrimary,
        type === 'code' && styles.code,
        type === 'wordmark' && styles.wordmark,
        type === 'sectionLabel' && styles.sectionLabel,
        type === 'stateLabel' && styles.stateLabel,
        type === 'rowTitle' && styles.rowTitle,
        type === 'rowValue' && styles.rowValue,
        style,
      ]}
      {...rest}
    />
  );
}

const styles = StyleSheet.create({
  small: {
    fontFamily: FontFamily.bodyMedium,
    fontSize: 14,
    lineHeight: 20,
  },
  smallBold: {
    fontFamily: FontFamily.bodyBold,
    fontSize: 14,
    lineHeight: 20,
  },
  default: {
    fontFamily: FontFamily.bodyMedium,
    fontSize: 16,
    lineHeight: 24,
  },
  title: {
    fontFamily: FontFamily.displayBold,
    fontSize: 48,
    lineHeight: 52,
  },
  subtitle: {
    fontFamily: FontFamily.displaySemiBold,
    fontSize: 32,
    lineHeight: 44,
  },
  link: {
    fontFamily: FontFamily.bodyMedium,
    lineHeight: 30,
    fontSize: 14,
  },
  linkPrimary: {
    fontFamily: FontFamily.bodyMedium,
    lineHeight: 30,
    fontSize: 14,
    color: '#3c87f7',
  },
  code: {
    fontFamily: Fonts.mono,
    fontWeight: Platform.select({ android: 700 }) ?? 500,
    fontSize: 12,
  },
  wordmark: {
    fontFamily: FontFamily.displaySemiBold,
    fontSize: 12,
    lineHeight: 16,
    letterSpacing: 1.7,
    textTransform: 'uppercase',
  },
  sectionLabel: {
    fontFamily: FontFamily.displayBold,
    fontSize: 11,
    lineHeight: 14,
    letterSpacing: 1.3,
    textTransform: 'uppercase',
  },
  stateLabel: {
    fontFamily: FontFamily.displayBold,
    fontSize: 22,
    lineHeight: 28,
  },
  rowTitle: {
    fontFamily: FontFamily.bodySemiBold,
    fontSize: 15,
    lineHeight: 20,
  },
  rowValue: {
    fontFamily: FontFamily.bodyMedium,
    fontSize: 13,
    lineHeight: 18,
  },
});
