import React from 'react';
import { View, Text, Alert } from 'react-native';
import Tile from '../components/Tile';

export default function HomeScreen({ navigation }) {
  const logout = () => Alert.alert('Sign out?', '', [{ text: 'Cancel' }, { text: 'Sign out', onPress: () => navigation.reset({ routes: [{ name: 'Login' }] }) }]);
  return (
    <View>
      <Text>Welcome</Text>
      <Tile title="Test History" onPress={() => navigation.navigate('TestHistory')} />
      <Tile title="Pending Uploads" onPress={() => api.post('/uploads/sync')} />
      <Tile title="Sign out" onPress={logout} />
    </View>
  );
}
