import { NativeTabs } from 'expo-router/unstable-native-tabs';
import { Platform } from 'react-native';

import { useTheme } from '@/hooks/use-theme';

export default function AppTabs() {
  const theme = useTheme();

  return (
    <NativeTabs
      backgroundColor={Platform.OS === 'ios' ? undefined : theme.background}
      tintColor={theme.accent}
      indicatorColor={theme.accentSurface}
      labelStyle={{ color: theme.textSecondary, selected: { color: theme.accent } }}>
      <NativeTabs.Trigger
        name="index"
        disableAutomaticContentInsets
        contentStyle={{ backgroundColor: theme.background }}>
        <NativeTabs.Trigger.Label>Home</NativeTabs.Trigger.Label>
        <NativeTabs.Trigger.Icon
          sf={{ default: 'house', selected: 'house.fill' }}
          md={{ default: 'home', selected: 'home_filled' }}
        />
      </NativeTabs.Trigger>

      <NativeTabs.Trigger
        name="apps"
        disableAutomaticContentInsets
        contentStyle={{ backgroundColor: theme.background }}>
        <NativeTabs.Trigger.Label>Apps</NativeTabs.Trigger.Label>
        <NativeTabs.Trigger.Icon
          sf={{ default: 'square.grid.2x2', selected: 'square.grid.2x2.fill' }}
          md={{ default: 'apps', selected: 'apps' }}
        />
      </NativeTabs.Trigger>
    </NativeTabs>
  );
}
