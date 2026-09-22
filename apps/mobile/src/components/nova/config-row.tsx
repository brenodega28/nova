import { Pressable, StyleSheet, View } from 'react-native';

import { ChevronIcon, ConfigEntryIcon } from './icons';

import { ThemedText } from '@/components/themed-text';
import type { ConfigEntry } from '@/constants/assistant';
import { Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

export function ConfigRow({ entry }: { entry: ConfigEntry }) {
  const theme = useTheme();

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${entry.title}: ${entry.value}`}
      style={({ pressed }) => [
        styles.row,
        { backgroundColor: theme.backgroundElement, shadowColor: theme.accent },
        pressed && styles.pressed,
      ]}>
      <View style={[styles.iconTile, { backgroundColor: theme.accentSurface }]}>
        <ConfigEntryIcon name={entry.icon} color={theme.accent} />
      </View>

      <View style={styles.labels}>
        <ThemedText type="rowTitle">{entry.title}</ThemedText>
        <ThemedText type="rowValue" themeColor="textSecondary" numberOfLines={1}>
          {entry.value}
        </ThemedText>
      </View>

      <ChevronIcon color={theme.chevron} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 14,
    paddingVertical: 14,
    paddingHorizontal: Spacing.three,
    borderRadius: Radius.card,
    minHeight: 70,
    shadowOffset: { width: 0, height: 8 },
    shadowOpacity: 0.08,
    shadowRadius: 16,
    elevation: 2,
  },
  pressed: {
    opacity: 0.7,
  },
  labels: {
    flex: 1,
    minWidth: 0,
    gap: Spacing.half,
  },
  iconTile: {
    width: 42,
    height: 42,
    borderRadius: Radius.icon,
    alignItems: 'center',
    justifyContent: 'center',
  },
});
