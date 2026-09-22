import { ScrollView, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ThemedText } from '@/components/themed-text';
import { HorizontalPadding, MaxContentWidth, Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

export default function AppsScreen() {
  const theme = useTheme();
  const safeAreaInsets = useSafeAreaInsets();

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
        <ThemedText type="stateLabel" style={styles.heading}>
          Apps &amp; Skills
        </ThemedText>
        <ThemedText type="rowValue" themeColor="textSecondary">
          Extend Nova with new abilities
        </ThemedText>

        <View
          style={[
            styles.placeholder,
            { backgroundColor: theme.backgroundElement, borderColor: theme.border },
          ]}>
          <ThemedText type="rowTitle">Nothing here yet</ThemedText>
          <ThemedText type="rowValue" themeColor="textSecondary">
            The skills catalog lands in a later pass.
          </ThemedText>
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
  },
  heading: {
    marginTop: Spacing.two,
  },
  placeholder: {
    marginTop: Spacing.four,
    padding: Spacing.four,
    borderRadius: Radius.card,
    borderWidth: 1,
    gap: Spacing.one,
  },
});
