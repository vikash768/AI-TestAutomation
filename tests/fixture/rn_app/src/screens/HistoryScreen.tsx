import React from 'react';
import { FlatList, Text, TouchableOpacity } from 'react-native';

export default function HistoryScreen({ tests, navigation }) {
  return (
    <>
      <Text>Tests you have run</Text>
      {tests.map(t => <TouchableOpacity key={t.id} onPress={() => navigation.navigate('Result')}><Text>Open result</Text></TouchableOpacity>)}
      {tests.length === 0 && <Text>No tests yet</Text>}
      <TouchableOpacity onPress={() => api.delete('/tests')}><Text>Clear history</Text></TouchableOpacity>
    </>
  );
}
