import { ScrollView, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { AssistantAvatar } from '@/components/nova/assistant-avatar';
import { ConfigRow } from '@/components/nova/config-row';
import { ThemedText } from '@/components/themed-text';
import {
  AssistantStateCopy,
  MockedAssistantState,
  MockedConfiguration,
} from '@/constants/assistant';
import { HorizontalPadding, MaxContentWidth, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

export default function HomeScreen() {
  const theme = useTheme();
  const safeAreaInsets = useSafeAreaInsets();
  const state = MockedAssistantState;
  const copy = AssistantStateCopy[state];

  return (
    <ScrollView
      style={[styles.scrollView, { backgroundColor: theme.background }]}
      contentInsetAdjustmentBehavior="never"
      automaticallyAdjustContentInsets={false}
      contentContainerStyle={[styles.contentContainer, { paddingTop: safeAreaInsets.top }]}>
      <View style={styles.content}>
        <ThemedText type="wordmark" themeColor="textSecondary">
          Nova
        </ThemedText>

        <View style={styles.hero}>
          <AssistantAvatar pulsing={state === 'listening'} />

          <View style={styles.heroCopy}>
            <ThemedText type="stateLabel">{copy.label}</ThemedText>
            <ThemedText type="rowValue" themeColor="textSecondary" style={styles.caption}>
              {copy.caption}
            </ThemedText>
          </View>
        </View>

        <View style={styles.section}>
          <ThemedText type="sectionLabel" themeColor="textSecondary">
            Configuration
          </ThemedText>

          <View style={styles.rows}>
            {MockedConfiguration.map((entry) => (
              <ConfigRow key={entry.key} entry={entry} />
            ))}
          </View>
        </View>
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  scrollView: {
    flex: 1,
  },
  contentContainer: {
    flexGrow: 1,
    alignItems: 'center',
    paddingHorizontal: HorizontalPadding,
  },
  content: {
    width: '100%',
    maxWidth: MaxContentWidth,
    alignItems: 'center',
  },
  hero: {
    alignItems: 'center',
    marginTop: 52,
  },
  heroCopy: {
    alignItems: 'center',
    marginTop: 26,
    gap: Spacing.one,
  },
  caption: {
    fontSize: 14,
    lineHeight: 20,
  },
  section: {
    alignSelf: 'stretch',
    marginTop: 44,
    gap: 14,
  },
  rows: {
    gap: 12,
  },
});
